"""Practo profile gateway, Playwright-backed.

Why Playwright (and not the Apify actor we tried first)
-------------------------------------------------------
Apify's `easyapi/practo-doctor-scraper` only accepts *search* URLs and
returns a flattened doctor card. It can't be pointed at a specific
doctor's profile URL, can't surface the Practo Plus badge or
slot-availability signals, and costs ~$0.005 per record.

We confirmed empirically (2026-04) that Practo's Akamai layer can be
passed by a real Chromium running with two trivial tweaks:

1. `--disable-blink-features=AutomationControlled` (hides the CDP
   "automation" marker)
2. `Object.defineProperty(navigator, "webdriver", {get: () => undefined})`
   patched in via an init script

…provided the browser uses Chrome's modern headless mode (`--headless=new`),
which shares the rendering pipeline of headful Chrome and is thus
indistinguishable to fingerprinting. Old `headless: true` ships a
separate "headless_shell" pipeline and IS detectable.

What this gateway does
----------------------
Navigates to a Practo doctor (or clinic) profile URL, waits for the
React/Backbone state to hydrate, then extracts data from two stable
sources:

- `window.__REDUX_STATE__` — Practo's full Redux store. Rich
  structured data: practitioner identity, qualifications, awards,
  memberships, the related clinic (`relations[0]`), fees, timings,
  rating, recommendation, prime/Plus flag, next-available timestamp.
- `<script type="application/ld+json">` blocks — schema.org JSON-LD
  for `Dentist` / `Physician` / `LocalBusiness`. Stable, public,
  used as a fallback for fields the Redux store omits.

Anti-block detection
--------------------
- Page title `Challenge Validation` → Akamai bot challenge.
- HTML body containing `Challenge Validation` (server-rendered shell).
- `__REDUX_STATE__` missing or empty → page didn't hydrate (often a
  challenge in disguise).
- Generic Playwright timeout → returned as `error`.

Returns a `PractoFetchResult` whose `profile.fetch_status` mirrors the
outcome (`ok` / `not_found` / `blocked` / `error`). Never raises for
HTTP / DOM / parsing errors during a fetch — those become `error`
results so the caller's loop stays uniform.

Rate-limit posture
------------------
Caller (controller) sleeps between calls; this gateway only adds a
small post-navigation settle (~1.5–2.5 s) so the React app has time
to hydrate before we read state.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from zelda.models.practo_profile import PractoFetchStatus, PractoProfile


_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

# Akamai's challenge page returns a near-empty document with this title.
_CHALLENGE_TITLE = "Challenge Validation"

# Strip the `navigator.webdriver` flag at context-init time. Combined
# with `--disable-blink-features=AutomationControlled` and Chrome's
# new headless mode (`--headless=new`), this passes Practo's Akamai.
_STEALTH_INIT_SCRIPT = (
    'Object.defineProperty(navigator, "webdriver", {get: () => undefined});'
)


# ── result type ─────────────────────────────────────────────────────


@dataclass
class PractoFetchResult:
    """Outcome of one profile fetch.

    `profile` is non-None for terminal-status results (`ok`,
    `not_found`, `blocked`, `error`) — even when the fetch failed,
    we return a stub-shaped profile recording the outcome so the
    caller can persist it without checking for None.
    """

    practo_url: str
    fetched_at: datetime
    status: PractoFetchStatus
    profile: PractoProfile
    error_message: str | None = None
    final_url: str | None = None


# ── gateway ────────────────────────────────────────────────────────


class PractoPlaywrightGateway:
    """Playwright-backed Practo scraper.

    Use as a context manager:

        with PractoPlaywrightGateway.launch() as gw:
            result = gw.fetch_profile(
                place_id="ChIJ_X",
                practo_url="https://www.practo.com/bangalore/doctor/dr-x",
            )
    """

    def __init__(
        self,
        *,
        playwright: Playwright,
        browser: Browser,
        page_load_timeout_ms: int = 30_000,
        post_load_settle_range_s: tuple[float, float] = (1.5, 2.5),
        user_agent: str | None = None,
        viewport: tuple[int, int] = (1366, 900),
    ) -> None:
        self._playwright = playwright
        self._browser = browser
        self._page_load_timeout_ms = page_load_timeout_ms
        self._post_load_settle_range_s = post_load_settle_range_s
        self._user_agent = user_agent or _DEFAULT_USER_AGENT
        self._viewport = viewport
        self._context: BrowserContext | None = None

    @classmethod
    def launch(cls, **kwargs: Any) -> "PractoPlaywrightGateway":
        """Spawn a Playwright + Chromium pair tuned for Practo's Akamai.

        We pass `headless=False` to Playwright but add `--headless=new`
        to the Chrome args ourselves. That combination uses Chrome's
        modern headless mode (which shares the rendering pipeline with
        headful Chrome and is indistinguishable to fingerprinting),
        rather than the older `headless_shell` binary that Playwright
        defaults to when `headless=True` is passed.
        """
        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--headless=new",
                "--no-sandbox",
            ],
        )
        return cls(playwright=pw, browser=browser, **kwargs)

    # ── lifecycle ────────────────────────────────────────────────────

    def __enter__(self) -> "PractoPlaywrightGateway":
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
        """Drop and recreate the BrowserContext to flush cookies /
        accumulated fingerprint signals. Caller (controller) decides
        cadence — typically every N profiles."""
        if self._context is not None:
            try:
                self._context.close()
            except Exception:  # noqa: BLE001
                pass
        self._context = self._browser.new_context(
            user_agent=self._user_agent,
            viewport={"width": self._viewport[0], "height": self._viewport[1]},
            locale="en-US",
        )
        self._context.add_init_script(_STEALTH_INIT_SCRIPT)

    def _ensure_context(self) -> BrowserContext:
        if self._context is None:
            self.reset_context()
        assert self._context is not None
        return self._context

    # ── public API ──────────────────────────────────────────────────

    def fetch_profile(
        self,
        *,
        place_id: str,
        practo_url: str,
        now: datetime | None = None,
    ) -> PractoFetchResult:
        """Navigate to `practo_url` and parse the profile.

        Always returns a `PractoFetchResult`. Inspect `.status`:
        - `ok`        — profile parsed cleanly.
        - `not_found` — page returned 404 / dead URL / no Redux state.
        - `blocked`   — Akamai challenge intercepted us.
        - `error`     — unexpected failure during navigation / parsing.
        """
        if not place_id or not place_id.strip():
            raise ValueError("place_id must be non-empty")
        if not practo_url or not practo_url.strip():
            raise ValueError("practo_url must be non-empty")

        fetched_at = now or datetime.now(timezone.utc)
        ctx = self._ensure_context()
        page = ctx.new_page()
        page.set_default_timeout(self._page_load_timeout_ms)

        final_url: str | None = None
        try:
            response = page.goto(practo_url, wait_until="domcontentloaded")
            final_url = page.url

            # Settle for the React app to hydrate __REDUX_STATE__.
            self._polite_sleep(*self._post_load_settle_range_s)

            title = page.title()
            if _is_challenge_page(title=title, html=page.content()):
                logger.warning(
                    "practo.fetch.blocked place_id={pid} url={u} title={t!r}",
                    pid=place_id, u=practo_url, t=title,
                )
                return _build_terminal_result(
                    place_id=place_id,
                    practo_url=practo_url,
                    fetched_at=fetched_at,
                    status="blocked",
                    error_message=f"akamai challenge page (title={title!r})",
                    final_url=final_url,
                )

            # 404-ish: Practo serves a thin page with no Redux state for
            # broken slugs. We handle that as `not_found` below.
            redux_state = page.evaluate("window.__REDUX_STATE__ || null")
            jsonld = page.evaluate(_JSONLD_EXTRACTOR_JS)

            if not isinstance(redux_state, dict) or not redux_state.get(
                "profile_reducer"
            ):
                # No state — could be 404 or a soft challenge.
                http_status = response.status if response else None
                logger.info(
                    "practo.fetch.no_state place_id={pid} url={u} http={s}",
                    pid=place_id, u=practo_url, s=http_status,
                )
                return _build_terminal_result(
                    place_id=place_id,
                    practo_url=practo_url,
                    fetched_at=fetched_at,
                    status="not_found",
                    error_message=(
                        f"no profile_reducer in __REDUX_STATE__ (http={http_status})"
                    ),
                    final_url=final_url,
                )

            profile = parse_practo_state(
                redux_state,
                jsonld_blocks=jsonld if isinstance(jsonld, list) else [],
                place_id=place_id,
                practo_url=practo_url,
                fetched_at=fetched_at,
            )

            logger.info(
                "practo.fetch.ok place_id={pid} name={name!r} fee={fee} "
                "recommend={rec}% reviews={r} prime={p}",
                pid=place_id,
                name=profile.name,
                fee=profile.consultation_fee,
                rec=profile.recommendation_percent,
                r=profile.reviews_count,
                p=profile.has_practo_plus_badge,
            )

            return PractoFetchResult(
                practo_url=practo_url,
                fetched_at=fetched_at,
                status="ok",
                profile=profile,
                final_url=final_url,
            )

        except PlaywrightTimeoutError as e:
            logger.error(
                "practo.fetch.timeout place_id={pid} url={u} err={e}",
                pid=place_id, u=practo_url, e=str(e),
            )
            return _build_terminal_result(
                place_id=place_id,
                practo_url=practo_url,
                fetched_at=fetched_at,
                status="error",
                error_message=f"playwright timeout: {e}",
                final_url=final_url,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                "practo.fetch.error place_id={pid} url={u} err={e}",
                pid=place_id, u=practo_url, e=f"{type(e).__name__}: {e}",
            )
            return _build_terminal_result(
                place_id=place_id,
                practo_url=practo_url,
                fetched_at=fetched_at,
                status="error",
                error_message=f"{type(e).__name__}: {e}",
                final_url=final_url,
            )
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass

    # ── internals ───────────────────────────────────────────────────

    def _polite_sleep(self, low: float, high: float) -> None:
        if high <= low:
            time.sleep(max(0.0, low))
            return
        time.sleep(random.uniform(low, high))


# JS that runs in the page context to pull every JSON-LD block. Returns
# a list of parsed objects (or arrays of objects). Errors per-block are
# silently dropped so a single malformed block doesn't break the rest.
_JSONLD_EXTRACTOR_JS = r"""
() => {
  const out = [];
  const nodes = document.querySelectorAll(
    "script[type='application/ld+json']"
  );
  nodes.forEach(n => {
    const txt = n.textContent || '';
    if (!txt.trim()) return;
    try { out.push(JSON.parse(txt)); } catch (e) { /* skip */ }
  });
  return out;
}
"""


# ── pure parsing helpers (unit-testable, no Playwright) ────────────


def _is_challenge_page(*, title: str, html: str) -> bool:
    """Detect Akamai's bot-challenge interstitial.

    Both the title and the visible body shell contain "Challenge
    Validation" on every flavour of the challenge we've seen. Body
    HTML is short (~1.8 KB) compared to a real profile (>500 KB),
    but the size signal is wobbly, so we go by content."""
    if title and _CHALLENGE_TITLE in title:
        return True
    if _CHALLENGE_TITLE in (html or ""):
        return True
    return False


def parse_practo_state(
    redux_state: dict[str, Any],
    *,
    jsonld_blocks: list[Any] | None = None,
    place_id: str,
    practo_url: str,
    fetched_at: datetime,
) -> PractoProfile:
    """Build a `PractoProfile` from Practo's `__REDUX_STATE__` plus
    optional JSON-LD blocks.

    The Redux store is the primary source — its field names are
    snake_case and match Practo's API. JSON-LD is a fallback for
    fields the store omits (e.g. aggregateRating on some pages,
    free-text descriptions).

    Pure function — no Playwright, easy to unit-test against fixtures.
    """
    jsonld_blocks = jsonld_blocks or []
    prof = redux_state.get("profile_reducer") or {}
    if not isinstance(prof, dict):
        prof = {}

    relations = prof.get("relations") or []
    rel = relations[0] if relations and isinstance(relations[0], dict) else {}

    establishment = rel.get("establishment") or {}
    if not isinstance(establishment, dict):
        establishment = {}

    address = establishment.get("address") or {}
    if not isinstance(address, dict):
        address = {}

    external_data = prof.get("external_data") or {}
    if not isinstance(external_data, dict):
        external_data = {}
    recommendation = external_data.get("recommendation") or {}
    if not isinstance(recommendation, dict):
        recommendation = {}

    establishment_rating = rel.get("establishment_rating") or {}
    if not isinstance(establishment_rating, dict):
        establishment_rating = {}

    fees_list = rel.get("fees") or []
    consult_fee_entry = next(
        (
            f for f in fees_list
            if isinstance(f, dict) and (f.get("type") == "CONSULTATION" or "amount" in f)
        ),
        {} if not fees_list else (fees_list[0] if isinstance(fees_list[0], dict) else {}),
    )

    # Currency comes from the address's country block; fall back to
    # INR (Practo is India-only).
    currency = (
        ((address.get("country") or {}).get("currency"))
        or "INR"
    )

    qualifications_raw = prof.get("qualifications") or []
    if not isinstance(qualifications_raw, list):
        qualifications_raw = []
    qualifications: list[str] = []
    for q in qualifications_raw:
        if not isinstance(q, dict):
            continue
        name = ((q.get("master_qualification") or {}).get("name")) or q.get(
            "name"
        )
        if isinstance(name, str) and name.strip():
            qualifications.append(name.strip())

    # JSON-LD pull — useful for some fields if Redux is sparse.
    dentist_jsonld = _find_jsonld(jsonld_blocks, "Dentist", "Physician", "LocalBusiness")

    summary = (
        ((prof.get("seo_data") or {}).get("description"))
        or (dentist_jsonld.get("description") if dentist_jsonld else None)
    )

    photos = establishment.get("photos") or []
    photo_urls: list[str] = []
    for p in photos:
        if isinstance(p, dict):
            for key in ("url", "src", "image_url"):
                v = p.get(key)
                if isinstance(v, str) and v.strip():
                    photo_urls.append(v.strip())
                    break
        elif isinstance(p, str) and p.strip():
            photo_urls.append(p.strip())

    # is_prime_doctor → has_practo_plus_badge. We treat None as "field
    # absent / unknown" rather than False.
    raw_prime = rel.get("is_prime_doctor")
    has_plus = bool(raw_prime) if isinstance(raw_prime, bool) else None

    next_available_at = _parse_iso_datetime(
        ((rel.get("availability_info") or {}).get("next_available_timestamp"))
    )

    return PractoProfile(
        place_id=place_id,
        practo_url=practo_url,
        practo_doctor_id=_first_str(prof, "fabric_id", "id"),
        profile_url=_first_str(prof, "profile_url"),
        name=_first_str(prof, "full_name"),
        qualifications=qualifications,
        experience_years=_first_int(
            prof, "years_of_experience", "experience_years", "experience"
        ),
        specializations=_extract_specializations(prof, dentist_jsonld),
        languages=_extract_languages(prof, rel),
        registrations=_clean_dict_list(prof.get("registrations")),
        education=_clean_dict_list(qualifications_raw),
        awards=_clean_dict_list(prof.get("awards")),
        memberships=_extract_membership_names(prof.get("memberships")),
        clinic_name=_first_str(establishment, "name"),
        clinic_address=_join_address_lines(address),
        clinic_locality=((address.get("locality") or {}).get("name")) if isinstance(
            address.get("locality"), dict
        ) else None,
        clinic_city=((address.get("city") or {}).get("city_name")) if isinstance(
            address.get("city"), dict
        ) else None,
        consultation_fee=_first_int(consult_fee_entry, "amount") or _first_int(
            rel, "consultation_fee"
        ),
        consultation_fee_currency=currency if consult_fee_entry else None,
        services=_extract_services(prof.get("services")),
        operating_hours=rel.get("timings") or establishment.get("timings") or None,
        lat=_first_float(address, "latitude") or _first_float(prof, "lat"),
        lng=_first_float(address, "longitude") or _first_float(prof, "lng"),
        recommendation_percent=_first_int(
            recommendation, "recommendation_percent"
        ),
        rating=_first_float(establishment_rating, "clinic_rating", "doctor_rating"),
        reviews_count=_first_int(
            recommendation, "response_count"
        ) or _first_int(
            establishment_rating, "total_recommendations"
        ),
        patient_count=_first_int(recommendation, "response_count"),
        has_practo_plus_badge=has_plus,
        next_available_at=next_available_at,
        profile_image_url=_first_str(
            prof, "enhanced_image_url", "image_url"
        ),
        photo_urls=photo_urls,
        summary=summary,
        fetch_status="ok",
        fetched_at=fetched_at,
        raw_json={
            "profile_reducer": prof,
            "relations_first": rel,
            "jsonld": jsonld_blocks,
        },
        discovered_at=fetched_at,  # controller overwrites if existing row
        last_modified_at=fetched_at,
    )


# ── extractor helpers ──────────────────────────────────────────────


def _build_terminal_result(
    *,
    place_id: str,
    practo_url: str,
    fetched_at: datetime,
    status: PractoFetchStatus,
    error_message: str | None,
    final_url: str | None,
) -> PractoFetchResult:
    """Build a fetch result for non-`ok` outcomes. Carries a stub-shape
    profile so the caller can upsert without checking for None."""
    profile = PractoProfile(
        place_id=place_id,
        practo_url=practo_url,
        fetch_status=status,
        fetched_at=fetched_at,
        error_message=error_message,
        discovered_at=fetched_at,
        last_modified_at=fetched_at,
    )
    return PractoFetchResult(
        practo_url=practo_url,
        fetched_at=fetched_at,
        status=status,
        profile=profile,
        error_message=error_message,
        final_url=final_url,
    )


def _find_jsonld(
    blocks: list[Any], *types: str
) -> dict[str, Any] | None:
    """First JSON-LD object whose `@type` matches any of `types`.
    Walks one level into list-shaped blocks (some pages emit the
    JSON-LD as a one-element list)."""
    type_set = {t.lower() for t in types}
    for blk in blocks:
        if isinstance(blk, dict):
            t = blk.get("@type")
            if isinstance(t, str) and t.lower() in type_set:
                return blk
        elif isinstance(blk, list):
            for item in blk:
                if isinstance(item, dict):
                    t = item.get("@type")
                    if isinstance(t, str) and t.lower() in type_set:
                        return item
    return None


def _extract_specializations(
    prof: dict[str, Any], jsonld: dict[str, Any] | None
) -> list[str]:
    """Specializations live in several places; try each in order.

    Practo's Redux store puts subspecialty data in `specializations[*].
    subspeciality.sub_speciality_name`. JSON-LD's `medicalSpecialty`
    is a fallback — it's a comma-joined string like 'Dentist, Orthodontist'.
    """
    out: list[str] = []
    raw = prof.get("specializations") or []
    if isinstance(raw, list):
        for s in raw:
            if not isinstance(s, dict):
                continue
            sub = s.get("subspeciality")
            if isinstance(sub, dict):
                name = sub.get("sub_speciality_name")
                if isinstance(name, str) and name.strip():
                    out.append(name.strip())
                    continue
            ms = s.get("master_specialization")
            if isinstance(ms, dict):
                speciality = ms.get("speciality")
                if isinstance(speciality, dict):
                    nm = speciality.get("speciality_name")
                    if isinstance(nm, str) and nm.strip():
                        out.append(nm.strip())
    # Dedupe while preserving order.
    if out:
        seen: set[str] = set()
        deduped = []
        for s in out:
            if s not in seen:
                seen.add(s)
                deduped.append(s)
        return deduped

    if jsonld:
        spec = jsonld.get("medicalSpecialty")
        if isinstance(spec, str):
            return [s.strip() for s in spec.split(",") if s.strip()]
        if isinstance(spec, list):
            return [s.strip() for s in spec if isinstance(s, str) and s.strip()]
    return []


def _extract_languages(
    prof: dict[str, Any], rel: dict[str, Any]
) -> list[str]:
    """Languages are inconsistent across Practo's profile schema. Try
    several plausible locations; return [] if none populate."""
    for source in (prof, rel):
        v = source.get("languages") or source.get("language")
        if isinstance(v, list):
            out = [s for s in v if isinstance(s, str) and s.strip()]
            if out:
                return [s.strip() for s in out]
        elif isinstance(v, str) and v.strip():
            return [s.strip() for s in v.split(",") if s.strip()]
    return []


def _extract_services(value: Any) -> list[str]:
    """Practo services are dicts with a nested `service.name`. Top-level
    `name` / `title` is the fallback for other shapes (Apify-style flat
    string lists also pass through `_to_str_list`)."""
    if isinstance(value, list) and value and isinstance(value[0], dict):
        out: list[str] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            inner = item.get("service")
            if isinstance(inner, dict):
                nm = inner.get("name") or inner.get("sub_speciality_name")
                if isinstance(nm, str) and nm.strip():
                    out.append(nm.strip())
                    continue
            for key in ("name", "title", "label", "value"):
                v = item.get(key)
                if isinstance(v, str) and v.strip():
                    out.append(v.strip())
                    break
        return out
    return _to_str_list(value)


def _extract_membership_names(value: Any) -> list[str]:
    """Memberships in Redux are dicts with `council.name`. Surface just
    the name string."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for m in value:
        if not isinstance(m, dict):
            continue
        council = m.get("council")
        if isinstance(council, dict):
            name = council.get("name")
            if isinstance(name, str) and name.strip():
                out.append(name.strip())
                continue
        nm = m.get("name")
        if isinstance(nm, str) and nm.strip():
            out.append(nm.strip())
    return out


def _join_address_lines(address: dict[str, Any]) -> str | None:
    """Join `address_line1` + `address_line2` (Practo's keys) or
    `line1` + `line2` (the older Apify shape) into one string."""
    if not isinstance(address, dict):
        return None
    parts: list[str] = []
    for key in ("address_line1", "line1", "address_line2", "line2"):
        v = address.get(key)
        if isinstance(v, str) and v.strip() and v.strip() not in parts:
            parts.append(v.strip())
    return ", ".join(parts) if parts else None


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    # Practo emits e.g. "2026-04-27T04:30:00.000+0000". `fromisoformat`
    # in 3.12 handles "+00:00" but not "+0000" — normalize.
    if re.search(r"[+-]\d{4}$", s):
        s = s[:-5] + s[-5:-2] + ":" + s[-2:]
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# ── tiny coercion helpers ────────────────────────────────────────────


def _first_str(record: dict[str, Any], *keys: str) -> str | None:
    for k in keys:
        v = record.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return str(v)
    return None


def _first_int(record: dict[str, Any], *keys: str) -> int | None:
    for k in keys:
        v = record.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            return int(v)
        if isinstance(v, str):
            stripped = v.strip().replace(",", "")
            digits = ""
            for ch in stripped:
                if ch.isdigit() or (ch == "-" and not digits):
                    digits += ch
                else:
                    break
            if digits and digits != "-":
                try:
                    return int(digits)
                except ValueError:
                    pass
    return None


def _first_float(record: dict[str, Any], *keys: str) -> float | None:
    for k in keys:
        v = record.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v.strip().replace(",", ""))
            except ValueError:
                continue
    return None


def _to_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        if "," in s:
            return [p.strip() for p in s.split(",") if p.strip()]
        return [s]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                t = item.strip()
                if t:
                    out.append(t)
            elif isinstance(item, dict):
                for key in ("name", "title", "label", "value"):
                    inner = item.get(key)
                    if isinstance(inner, str) and inner.strip():
                        out.append(inner.strip())
                        break
        return out
    return []


def _clean_dict_list(value: Any) -> list[dict[str, Any]]:
    """Return only the dict entries from `value`. Strips noise so the
    stored `education` / `registrations` / `awards` lists are uniform."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
