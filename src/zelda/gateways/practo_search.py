"""Practo URL-discovery gateway, Playwright-backed.

Companion to `practo_playwright.PractoPlaywrightGateway`: that one
fetches *one* known profile URL; this one fetches Practo's city-wide
dentist listing and returns the candidates for the discovery
controller to fuzzy-match against.

Why "list, then match" (not "query Practo with the lead name")
--------------------------------------------------------------
We initially built this against Practo's structured-search endpoint,
`/search/doctors?city=X&q=[<json>]`, on the assumption that the `q`
parameter filtered candidates by clinic / doctor name. Live testing
(2026-04, Ludhiana) showed it does not: arbitrary clinic queries
("Saggar Dental", "Sai Dental Clinic") return the same generic
city-wide doctor list (urologists, gynecologists, audiologists)
regardless of the query. Practo's `q` only filters when it matches
their internal autocomplete dictionary (specialty terms like
"Dentist", or known doctor names) — clinic names usually don't.

Switching to `/<city-slug>/dentist` — Practo's per-city dentist
listing — returns ACTUAL dentists, ranked by Practo's own relevance.
The discovery controller's fuzzy match handles the per-lead filtering
client-side.

Two alternative URL shapes we rejected:
- `/<city-slug>/dentists?q=...` (the brief's guess; plural) → 404.
- `/search/doctors?city=X&q=...` → returns generic, see above.

Pagination
----------
The listing returns 10 entities per page. `search_dentists(...,
max_pages=N)` walks pages 1..N, deduping by entity ID; the
discovery controller's `max_candidates` then caps the union. Default
is `max_pages=3` (≤30 candidates) which covers small / mid cities
in full and the relevant top of the long tail in metros.

Signature deviation from the brief
----------------------------------
- We return a `PractoSearchOutcome` wrapper (status + candidates)
  rather than a bare `list[PractoSearchResult]`. The discovery
  controller needs to distinguish "search ok, no matches" from
  "search blocked by Akamai" — a bare list collapses the two.
- The `query` parameter is preserved on the signature for logging
  but unused inside (see "Why list, then match" above). The
  signature stays compatible with the orchestrator's calling
  convention.

Anti-block detection + rate-limit posture mirror the profile gateway:
see `_practo_browser` for the shared plumbing.
"""

from __future__ import annotations

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
    CHROMIUM_LAUNCH_ARGS,
    DEFAULT_USER_AGENT,
    DEFAULT_VIEWPORT,
    is_challenge_page,
    launch_chromium,
    make_context,
    polite_sleep,
)


_PRACTO_BASE = "https://www.practo.com"
# Per-city dentist listing — what Practo's UI calls "Dentists in <City>".
# Path is `/<city-slug>/dentist` (singular, despite the plural in the
# brief). We append `?page=N` for pagination.
_LISTING_PATH_TEMPLATE = "/{city_slug}/dentist"


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
        owns_playwright: bool = True,
    ) -> None:
        self._playwright = playwright
        self._browser = browser
        self._page_load_timeout_ms = page_load_timeout_ms
        self._post_load_settle_range_s = post_load_settle_range_s
        self._user_agent = user_agent or DEFAULT_USER_AGENT
        self._viewport = viewport
        # `owns_playwright=False` is used when the parent process is
        # sharing one Playwright across multiple gateways. Playwright's
        # Sync API only permits one runtime per process, so the
        # orchestrator / CLI starts one and passes it to every gateway;
        # only the owner stops it on close.
        self._owns_playwright = owns_playwright
        self._context: BrowserContext | None = None

        # Per-URL cache of successful listing fetches. The same Ludhiana
        # `?page=1` listing serves every Ludhiana lead, so without a
        # cache we'd re-fetch identical pages N times per city pass —
        # wasteful AND a behavior pattern Akamai might flag. Only `ok`
        # outcomes are cached; failures retry on next call.
        self._listing_cache: dict[str, PractoSearchOutcome] = {}

    @classmethod
    def launch(
        cls,
        *,
        playwright: Playwright | None = None,
        **kwargs: Any,
    ) -> "PractoSearchGateway":
        """Spawn a Playwright + Chromium pair tuned for Practo's Akamai
        (see `_practo_browser.launch_chromium`).

        Pass `playwright=<existing>` to share a Playwright runtime with
        another gateway in the same process — Playwright Sync API only
        permits one runtime per process.
        """
        if playwright is None:
            pw, browser = launch_chromium()
            owns = True
        else:
            pw = playwright
            browser = playwright.chromium.launch(
                headless=False,
                args=list(CHROMIUM_LAUNCH_ARGS),
            )
            owns = False
        return cls(
            playwright=pw, browser=browser, owns_playwright=owns, **kwargs,
        )

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
        if self._owns_playwright:
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
        max_pages: int = 3,
        now: datetime | None = None,
    ) -> PractoSearchOutcome:
        """Fetch Practo's per-city dentist listing for `city_slug`.

        Walks pages 1..`max_pages`, accumulating candidates by their
        unique `id`, and returns up to `max_results` total. Stops
        early on Akamai block, generic errors, or when a page yields
        zero new candidates (end of listing).

        `query` is preserved on the signature for logging — Practo's
        listing endpoint does NOT filter by query in our testing, so
        client-side fuzzy matching (in the discovery controller) is
        what does the actual matching.
        """
        if not query or not query.strip():
            raise ValueError("query must be non-empty")
        if not city_slug or not city_slug.strip():
            raise ValueError("city_slug must be non-empty")
        if max_results < 1:
            raise ValueError("max_results must be >= 1")
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")

        searched_at = now or datetime.now(timezone.utc)
        first_page_url = build_listing_url(city_slug=city_slug, page=1)

        ctx = self._ensure_context()
        seen_ids: set[str] = set()
        merged: list[PractoSearchResult] = []
        last_final_url: str | None = None

        for page_num in range(1, max_pages + 1):
            url = build_listing_url(city_slug=city_slug, page=page_num)
            outcome = self._fetch_one_listing_page(
                url=url, query=query, city_slug=city_slug,
                searched_at=searched_at,
            )
            last_final_url = outcome.final_url or last_final_url

            if outcome.status != "ok":
                # Propagate the failure verbatim; preserve any candidates
                # already accumulated from earlier pages.
                outcome.candidates = merged
                outcome.search_url = first_page_url
                outcome.final_url = last_final_url
                return outcome

            new_count = 0
            for cand in outcome.candidates:
                # Dedup by Practo's stable doctor_id (in raw['doctor_id']
                # or raw['id']); fall back to URL.
                key = (
                    str(cand.raw.get("doctor_id") or cand.raw.get("id") or "")
                    or cand.practo_url
                )
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                merged.append(cand)
                new_count += 1
                if len(merged) >= max_results:
                    break

            logger.info(
                "practo.search.page query={q!r} city={c} page={p} "
                "new={n} total={t}",
                q=query, c=city_slug, p=page_num,
                n=new_count, t=len(merged),
            )

            if len(merged) >= max_results:
                break
            if new_count == 0:
                # Listing exhausted — no point fetching more pages.
                break

        return PractoSearchOutcome(
            query=query,
            city_slug=city_slug,
            searched_at=searched_at,
            status="ok",
            candidates=merged,
            final_url=last_final_url,
            search_url=first_page_url,
        )

    # ── internals ───────────────────────────────────────────────────

    def _fetch_one_listing_page(
        self,
        *,
        url: str,
        query: str,
        city_slug: str,
        searched_at: datetime,
    ) -> PractoSearchOutcome:
        """Fetch one page and parse it. Always returns an outcome.

        Reads from `self._listing_cache` first (only `ok` outcomes are
        cached) so multiple leads in the same city share fetches.
        """
        cached = self._listing_cache.get(url)
        if cached is not None:
            return cached

        ctx = self._ensure_context()
        page = ctx.new_page()
        page.set_default_timeout(self._page_load_timeout_ms)
        final_url: str | None = None
        try:
            page.goto(url, wait_until="domcontentloaded")
            final_url = page.url
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
                    search_url=url,
                )

            redux = page.evaluate("window.__REDUX_STATE__ || null")
            candidates = parse_search_state(
                redux if isinstance(redux, dict) else {},
                max_results=1_000,  # parser cap; outer loop applies max_results
            )
            outcome = PractoSearchOutcome(
                query=query,
                city_slug=city_slug,
                searched_at=searched_at,
                status="ok",
                candidates=candidates,
                final_url=final_url,
                search_url=url,
            )
            self._listing_cache[url] = outcome
            return outcome

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
                search_url=url,
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
                search_url=url,
            )
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass


# ── pure helpers (unit-testable, no Playwright) ────────────────────


def build_listing_url(*, city_slug: str, page: int = 1) -> str:
    """Build the URL for one page of Practo's per-city dentist listing.

    Pattern: `https://www.practo.com/<city-slug>/dentist[?page=N]`.
    Page 1 omits the `page` query param to match Practo's canonical
    URL exactly; pages 2+ append it.
    """
    if page < 1:
        raise ValueError("page must be >= 1")
    path = _LISTING_PATH_TEMPLATE.format(city_slug=city_slug.strip())
    if page == 1:
        return f"{_PRACTO_BASE}{path}"
    return f"{_PRACTO_BASE}{path}?{urlencode({'page': page})}"


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
