"""Practo URL-discovery gateway, Playwright-backed.

Companion to `practo_playwright.PractoPlaywrightGateway`: that one
fetches *one* known profile URL; this one *searches* Practo by
clinic/doctor name and returns candidate matches with enough
metadata for the discovery controller to score them.

Search URL pattern
------------------
We use Practo's structured-search endpoint, which accepts a
JSON-encoded query and a city parameter:

    https://www.practo.com/search/doctors
        ?city=<City>
        &q=[{"word":"<query>","autocompleted":false,"category":"doctor"}]

This was determined empirically (2026-04). Two alternatives we
ruled out:

- `/<city-slug>/dentists?q=...` — 404 (the brief's guess; plural
  form doesn't exist).
- `/<city-slug>/dentist?q=...` — returns 200 but the `?q=` is
  ignored; it serves a generic city listing of dentists.

Result extraction
-----------------
The SERP hydrates a Redux store on `window.__REDUX_STATE__` whose
`listingV2.doctors.entities` dict holds full per-doctor records
keyed by ID, and `listingV2.doctors.items` carries the relevance-
ranked ordering. We walk `items` in order and pick up to
`max_results` entities.

The brief specified that `search_dentists()` would return a bare
`list[PractoSearchResult]`. We diverge: we return a `PractoSearchOutcome`
wrapper carrying both the candidates AND a status enum, because the
discovery controller needs to distinguish "search worked, no
matches" from "search blocked by Akamai" — and a bare list collapses
the two. The orchestrator integrating these gateways already
expects `blocked / error / ok` semantics; matching that here keeps
the surface consistent.

Anti-block detection + rate-limit posture mirror the profile gateway:
see `_practo_browser` for the shared plumbing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from loguru import logger
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
)

from zelda.gateways._practo_browser import (
    DEFAULT_USER_AGENT,
    DEFAULT_VIEWPORT,
    is_challenge_page,
    launch_chromium,
    make_context,
    polite_sleep,
)


_PRACTO_BASE = "https://www.practo.com"
_SEARCH_PATH = "/search/doctors"


# ── result types ────────────────────────────────────────────────────


PractoSearchStatus = Literal["ok", "blocked", "error"]


@dataclass
class PractoSearchResult:
    """One candidate doctor profile from a Practo search SERP.

    Carries enough fields for the discovery controller to score
    against a lead's clinic name and (if matched) feed
    `repo.upsert_stub` with a clean URL.
    """

    practo_url: str
    """Absolute Practo profile URL, with search-context query
    parameters (`specialization`, `referrer`, `page_uid`) stripped.
    Only `practice_id` is preserved because the profile gateway
    uses it to disambiguate doctors who practice at multiple
    clinics."""

    doctor_name: str | None
    clinic_name: str | None
    specialization: str | None
    locality: str | None
    profile_image_url: str | None
    verified_badge: bool
    """True if the candidate has any Practo-paid badge surfaced
    on the SERP (Prime / Plus / Prime Basic / Prime Online).
    Useful as a tie-breaker but not a match-quality signal on
    its own."""

    raw: dict[str, Any] = field(default_factory=dict)
    """Source `entities[id]` dict — kept verbatim so the controller
    can pull additional fields (lat/lng, fees, etc.) without us
    having to anticipate every downstream need."""


@dataclass
class PractoSearchOutcome:
    """Outcome of one search call. Always returned (the gateway
    never raises for HTTP / DOM / parsing errors during a search —
    those become `status='error'` results so the controller's loop
    stays uniform).

    `status='ok'` does NOT imply non-empty `candidates` — Practo
    can return zero results for a query, which is a valid "no
    match" signal for the controller.
    """

    query: str
    city_slug: str
    searched_at: datetime
    status: PractoSearchStatus
    candidates: list[PractoSearchResult]
    error_message: str | None = None
    final_url: str | None = None
    search_url: str | None = None


# ── gateway ────────────────────────────────────────────────────────


class PractoSearchGateway:
    """Playwright-backed Practo SERP scraper.

    Use as a context manager:

        with PractoSearchGateway.launch() as gw:
            outcome = gw.search_dentists(
                query="Sai Dental Clinic", city_slug="ludhiana",
            )
            for cand in outcome.candidates:
                print(cand.practo_url, cand.doctor_name)
    """

    def __init__(
        self,
        *,
        playwright: Playwright,
        browser: Browser,
        page_load_timeout_ms: int = 30_000,
        post_load_settle_range_s: tuple[float, float] = (1.5, 2.5),
        user_agent: str | None = None,
        viewport: tuple[int, int] = DEFAULT_VIEWPORT,
    ) -> None:
        self._playwright = playwright
        self._browser = browser
        self._page_load_timeout_ms = page_load_timeout_ms
        self._post_load_settle_range_s = post_load_settle_range_s
        self._user_agent = user_agent or DEFAULT_USER_AGENT
        self._viewport = viewport
        self._context: BrowserContext | None = None

    @classmethod
    def launch(cls, **kwargs: Any) -> "PractoSearchGateway":
        """Spawn a Playwright + Chromium pair tuned for Practo's Akamai
        (see `_practo_browser.launch_chromium`)."""
        pw, browser = launch_chromium()
        return cls(playwright=pw, browser=browser, **kwargs)

    # ── lifecycle ───────────────────────────────────────────────────

    def __enter__(self) -> "PractoSearchGateway":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:  # noqa: BLE001
                pass
            self._context = None
        try:
            self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._playwright.stop()
        except Exception:  # noqa: BLE001
            pass

    def reset_context(self) -> None:
        """Drop and recreate the BrowserContext to flush cookies."""
        if self._context is not None:
            try:
                self._context.close()
            except Exception:  # noqa: BLE001
                pass
        self._context = make_context(
            self._browser,
            user_agent=self._user_agent,
            viewport=self._viewport,
        )

    def _ensure_context(self) -> BrowserContext:
        if self._context is None:
            self.reset_context()
        assert self._context is not None
        return self._context

    # ── public API ──────────────────────────────────────────────────

    def search_dentists(
        self,
        *,
        query: str,
        city_slug: str,
        max_results: int = 10,
        now: datetime | None = None,
    ) -> PractoSearchOutcome:
        """Search Practo for dentists matching `query` in `city_slug`.

        Returns up to `max_results` candidates ordered by Practo's
        own relevance ranking. Scoring against the lead's name is
        the controller's job — this gateway is intentionally
        unopinionated about which candidate is "right".
        """
        if not query or not query.strip():
            raise ValueError("query must be non-empty")
        if not city_slug or not city_slug.strip():
            raise ValueError("city_slug must be non-empty")
        if max_results < 1:
            raise ValueError("max_results must be >= 1")

        searched_at = now or datetime.now(timezone.utc)
        search_url = build_search_url(query=query, city_slug=city_slug)

        ctx = self._ensure_context()
        page = ctx.new_page()
        page.set_default_timeout(self._page_load_timeout_ms)

        final_url: str | None = None
        try:
            page.goto(search_url, wait_until="domcontentloaded")
            final_url = page.url

            # Settle so the React app hydrates __REDUX_STATE__.
            polite_sleep(*self._post_load_settle_range_s)

            title = page.title()
            if is_challenge_page(title=title, html=page.content()):
                logger.warning(
                    "practo.search.blocked query={q!r} city={c} title={t!r}",
                    q=query, c=city_slug, t=title,
                )
                return PractoSearchOutcome(
                    query=query,
                    city_slug=city_slug,
                    searched_at=searched_at,
                    status="blocked",
                    candidates=[],
                    error_message=f"akamai challenge page (title={title!r})",
                    final_url=final_url,
                    search_url=search_url,
                )

            redux = page.evaluate("window.__REDUX_STATE__ || null")
            candidates = parse_search_state(
                redux if isinstance(redux, dict) else {},
                max_results=max_results,
            )

            logger.info(
                "practo.search.ok query={q!r} city={c} candidates={n}",
                q=query, c=city_slug, n=len(candidates),
            )

            return PractoSearchOutcome(
                query=query,
                city_slug=city_slug,
                searched_at=searched_at,
                status="ok",
                candidates=candidates,
                final_url=final_url,
                search_url=search_url,
            )

        except PlaywrightTimeoutError as e:
            logger.error(
                "practo.search.timeout query={q!r} city={c} err={e}",
                q=query, c=city_slug, e=str(e),
            )
            return PractoSearchOutcome(
                query=query,
                city_slug=city_slug,
                searched_at=searched_at,
                status="error",
                candidates=[],
                error_message=f"playwright timeout: {e}",
                final_url=final_url,
                search_url=search_url,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                "practo.search.error query={q!r} city={c} err={e}",
                q=query, c=city_slug,
                e=f"{type(e).__name__}: {e}",
            )
            return PractoSearchOutcome(
                query=query,
                city_slug=city_slug,
                searched_at=searched_at,
                status="error",
                candidates=[],
                error_message=f"{type(e).__name__}: {e}",
                final_url=final_url,
                search_url=search_url,
            )
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass


# ── pure helpers (unit-testable, no Playwright) ────────────────────


def build_search_url(*, query: str, city_slug: str) -> str:
    """Build Practo's structured-search URL.

    Practo's `/search/doctors` endpoint expects:
    - `city`: title-case city name (e.g. "Ludhiana"). We derive this
      from the slug by `.title()`, which works for the merged-metro
      slugs Practo uses (e.g. "bangalore" → "Bangalore"). Hyphenated
      slugs would title-case wrong, but Practo doesn't use them.
    - `q`: a JSON-encoded list with one query object. The `category`
      field is "doctor" — we've found that yields the most usable
      candidates for clinic-name searches; "clinic" sometimes returns
      no doctor profiles at all.
    """
    q_payload = [{"word": query, "autocompleted": False, "category": "doctor"}]
    params = {
        "city": city_slug.strip().title(),
        "q": json.dumps(q_payload, separators=(",", ":")),
    }
    return f"{_PRACTO_BASE}{_SEARCH_PATH}?{urlencode(params)}"


def normalize_profile_url(rel_or_abs_url: str) -> str:
    """Take a `profile_url` from the SERP entity and return a clean
    absolute URL.

    Practo embeds search-context leakage into the `profile_url` field
    (`specialization=<query>`, `referrer=doctor_listing`, `page_uid=...`).
    We strip everything except `practice_id`, which is the only query
    param the profile page genuinely needs (it disambiguates doctors
    who practice at multiple clinics).
    """
    if not isinstance(rel_or_abs_url, str) or not rel_or_abs_url.strip():
        return ""
    parts = urlsplit(rel_or_abs_url.strip())
    qs = dict(parse_qsl(parts.query, keep_blank_values=False))
    keep = {k: v for k, v in qs.items() if k == "practice_id"}
    new_query = urlencode(keep)
    scheme = parts.scheme or "https"
    netloc = parts.netloc or "www.practo.com"
    return urlunsplit((scheme, netloc, parts.path, new_query, ""))


def parse_search_state(
    redux: dict[str, Any], *, max_results: int = 10
) -> list[PractoSearchResult]:
    """Extract candidates from `window.__REDUX_STATE__` of a Practo SERP.

    Walks `listingV2.doctors.items` in order (Practo's relevance
    ranking) and looks up each ID in `listingV2.doctors.entities`.
    Skips entities that lack a usable `profile_url`. Returns up to
    `max_results` candidates.
    """
    listing_v2 = redux.get("listingV2") if isinstance(redux, dict) else None
    if not isinstance(listing_v2, dict):
        return []

    doctors = listing_v2.get("doctors")
    if not isinstance(doctors, dict):
        return []

    entities = doctors.get("entities")
    items = doctors.get("items")
    if not isinstance(entities, dict) or not isinstance(items, list):
        return []

    out: list[PractoSearchResult] = []
    for item in items:
        if len(out) >= max_results:
            break
        eid = _item_id(item)
        if eid is None:
            continue
        ent = entities.get(eid) or entities.get(str(eid))
        if not isinstance(ent, dict):
            continue
        result = _entity_to_result(ent)
        if result is not None:
            out.append(result)
    return out


def _item_id(item: Any) -> str | None:
    """`items` entries are typically `{"id": <int>}` but we've seen
    bare ints / strings in older snapshots."""
    if isinstance(item, dict):
        v = item.get("id")
        return str(v) if v is not None else None
    if isinstance(item, (int, str)):
        return str(item)
    return None


def _entity_to_result(ent: dict[str, Any]) -> PractoSearchResult | None:
    """Build one `PractoSearchResult` from a SERP entity dict.

    Returns None when the entity lacks a usable profile URL — those
    are treated as unscoreable and the controller never sees them.
    """
    raw_url = ent.get("profile_url")
    if not isinstance(raw_url, str) or not raw_url.strip():
        return None

    practo_url = normalize_profile_url(raw_url)
    if not practo_url:
        return None

    practice = ent.get("practice") or {}
    if not isinstance(practice, dict):
        practice = {}

    doctor_name = _str_or_none(ent.get("doctor_name"))
    # `clinic_name` lives at the top level on most entities; fall back
    # to `practice.name` when absent.
    clinic_name = _str_or_none(ent.get("clinic_name")) or _str_or_none(
        practice.get("name")
    )
    specialization = _str_or_none(ent.get("specialization"))
    locality = _str_or_none(ent.get("locality")) or _str_or_none(
        practice.get("locality")
    )

    profile_image_url = _str_or_none(ent.get("image_url"))
    if profile_image_url is None:
        photo = ent.get("profile_photo")
        if isinstance(photo, dict):
            profile_image_url = _str_or_none(photo.get("url"))

    # Verified-badge proxy: any Practo-paid tier shown on the card.
    verified_badge = bool(
        ent.get("is_practo_prime")
        or ent.get("is_practo_prime_basic")
        or ent.get("is_practo_prime_online")
        or ent.get("is_prime_badge_enabled")
    )

    return PractoSearchResult(
        practo_url=practo_url,
        doctor_name=doctor_name,
        clinic_name=clinic_name,
        specialization=specialization,
        locality=locality,
        profile_image_url=profile_image_url,
        verified_badge=verified_badge,
        raw=ent,
    )


def _str_or_none(value: Any) -> str | None:
    if isinstance(value, str):
        s = value.strip()
        return s or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None
