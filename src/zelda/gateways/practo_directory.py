"""`PractoDirectoryGateway` — paginates Practo's per-city dental-clinic
directory and returns one `PractoDirectoryEntry` per clinic card.

Why a dedicated gateway?
- Practo's listing pages are server-rendered HTML with the
  per-clinic data embedded as a quasi-JSON blob (not full JSON-LD).
- The pages are gated by Akamai when the request lacks a real-
  browser User-Agent. With a normal Chrome UA they serve cleanly
  over plain HTTP — no Playwright needed.
- Every other access pattern (directly fetching a profile page,
  scraping search results) needs different scaffolding. Keeping the
  directory crawl in its own gateway keeps the discovery step thin.

The gateway returns a simple dataclass (`PractoDirectoryEntry`) — not
the persistence model `PractoListing`. Discovery-time housekeeping
fields (`city`, `discovered_at`, `last_modified_at`) live one layer
up at the controller; the gateway only knows what Practo told it.
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass

import httpx
from loguru import logger


PRACTO_BASE_URL = "https://www.practo.com"


# Practo's path-segment city slugs differ from the Places-API city
# names in a handful of merged-metro / renamed-city cases.
_CITY_OVERRIDES: dict[str, str] = {
    "bengaluru": "bangalore",
    "new delhi": "delhi",
    "calcutta": "kolkata",
    "bombay": "mumbai",
    "madras": "chennai",
    "gurugram": "gurgaon",
}


def practo_city_slug(city: str) -> str:
    """Normalize a Places-API city name to Practo's URL slug.

    Lowercase, collapse whitespace, then look up in the override
    dict; fall back to spaces→hyphens for unlisted cities. Returns
    "" for empty / whitespace-only input.
    """
    if not city:
        return ""
    norm = " ".join(city.strip().lower().split())
    if not norm:
        return ""
    return _CITY_OVERRIDES.get(norm, norm.replace(" ", "-"))


# Realistic browser fingerprint — Akamai bot challenges hide the
# listing markup if the UA looks scripted.
_DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# Each clinic card on a listing page emits a quasi-JSON blob with
# `"name":"X" ... "streetAddress":"Y" ... "latitude":N "longitude":N`.
# The card's own `"url":"…/clinic/<slug>"` appears soon after `"name"`
# (before the latlng), so we anchor on the name and scan forward in
# a bounded window for the URL.
_CLINIC_PATTERN = re.compile(
    r'"name":"([^"]+)"'
    r'.{0,3000}?"streetAddress":"([^"]*)"'
    r'.{0,500}?"latitude":([0-9.\-]+)'
    r'.{0,200}?"longitude":([0-9.\-]+)',
    re.DOTALL,
)
_CARD_URL_PATTERN = re.compile(
    r'"url":"(https?:\\?/\\?/[^"]*?/clinic/[a-z0-9\-]+)"'
)
# The next card's start. We bound the URL search at this so we
# never read past a sibling card.
_NEXT_NAME_PATTERN = re.compile(r'"name":"')
# A card's own URL appears within ~4 KB of the card's `"name"` field.
_URL_LOOKAHEAD_BYTES = 4000


@dataclass(frozen=True)
class PractoDirectoryEntry:
    """One clinic card as returned by the gateway. Discovery-time
    fields (city, discovered_at) are added by the controller."""

    profile_url: str
    name: str
    address: str
    lat: float | None
    lng: float | None


class PractoDirectoryGateway:
    """Paginates Practo's per-city dental-clinic directory."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 30.0,
        max_pages: int = 15,
    ) -> None:
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        self._max_pages = max_pages
        self._owns_client = client is None
        self._client = client or httpx.Client(
            headers=_DEFAULT_HEADERS,
            timeout=timeout_seconds,
            follow_redirects=True,
        )

    def __enter__(self) -> "PractoDirectoryGateway":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def fetch_for_city(self, city: str) -> list[PractoDirectoryEntry]:
        """Return every dental-clinic listing for `city`, deduped by
        profile URL.

        Pagination terminates as soon as a page yields no new
        entries (Practo wraps back to earlier results past the
        actual page count). `max_pages` is a hard ceiling.

        On HTTP / parse errors: logs and returns whatever was
        collected so far. Never raises for per-page issues — the
        discovery step needs to make a decision even with partial
        data.
        """
        slug = practo_city_slug(city)
        if not slug:
            logger.warning("practo_directory.no_slug city={c}", c=city)
            return []

        seen_urls: set[str] = set()
        out: list[PractoDirectoryEntry] = []

        for page in range(1, self._max_pages + 1):
            url = f"{PRACTO_BASE_URL}/{slug}/clinics/dental-clinics?page={page}"
            html = self._fetch_page(url)
            if html is None:
                break  # hard error; keep what we have

            page_entries = list(_extract_entries(html, city_slug=slug))
            new_for_page = 0
            for entry in page_entries:
                if entry.profile_url in seen_urls:
                    continue
                seen_urls.add(entry.profile_url)
                out.append(entry)
                new_for_page += 1

            logger.info(
                "practo_directory.page city={c} page={p} found={f} new={n}",
                c=city, p=page, f=len(page_entries), n=new_for_page,
            )

            # Saturation: a page that introduced zero new entries
            # means Practo has wrapped back to earlier results.
            if new_for_page == 0:
                break

        logger.info(
            "practo_directory.done city={c} total_unique={n}",
            c=city, n=len(out),
        )
        return out

    def _fetch_page(self, url: str) -> str | None:
        try:
            resp = self._client.get(url)
        except httpx.HTTPError as e:
            logger.error(
                "practo_directory.fetch_error url={u} err={e}", u=url, e=e,
            )
            return None
        if resp.status_code != 200:
            logger.warning(
                "practo_directory.bad_status url={u} status={s}",
                u=url, s=resp.status_code,
            )
            return None
        return resp.text


def _extract_entries(
    html: str, *, city_slug: str
) -> list[PractoDirectoryEntry]:
    """Pull `(profile_url, name, address, lat, lng)` records from one
    listing page's HTML.

    The `city_slug` argument filters out cross-city promo cards that
    Practo occasionally injects.
    """
    out: list[PractoDirectoryEntry] = []
    seen_urls: set[str] = set()

    for m in _CLINIC_PATTERN.finditer(html):
        try:
            lat = float(m.group(3))
            lng = float(m.group(4))
        except ValueError:
            continue

        # Search forward from the start of "name" for the card's own
        # /clinic/ URL. Stop at the next `"name":` so we never spill
        # into a sibling card.
        window_end = m.end() + _URL_LOOKAHEAD_BYTES
        next_name = _NEXT_NAME_PATTERN.search(html, m.end())
        if next_name and next_name.start() < window_end:
            window_end = next_name.start()
        window = html[m.start():window_end]

        url_match = _CARD_URL_PATTERN.search(window)
        if not url_match:
            continue
        profile_url = url_match.group(1).replace("\\/", "/")

        if f"/{city_slug}/clinic/" not in profile_url:
            continue
        if profile_url in seen_urls:
            continue
        seen_urls.add(profile_url)

        out.append(PractoDirectoryEntry(
            profile_url=profile_url,
            name=_decode(m.group(1)),
            address=_decode(m.group(2)),
            lat=lat,
            lng=lng,
        ))
    return out


def _decode(s: str) -> str:
    """HTML-unescape and \\u-decode a Practo string field."""
    if not s:
        return ""
    out = _html.unescape(s)
    try:
        out = out.encode("utf-8").decode("unicode_escape")
    except UnicodeDecodeError:
        pass
    return out


__all__ = [
    "PRACTO_BASE_URL",
    "PractoDirectoryEntry",
    "PractoDirectoryGateway",
    "practo_city_slug",
]
