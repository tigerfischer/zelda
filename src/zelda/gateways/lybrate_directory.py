"""`LybrateDirectoryGateway` — paginates Lybrate's per-city dentist
directory and returns one `LybrateDirectoryEntry` per doctor card.

Why this gateway is so much smaller than the Practo one
-------------------------------------------------------
Lybrate ships full schema.org `Physician` JSON-LD inside
`<script type="application/ld+json">` blocks. Each block parses
cleanly as JSON; no regex tricks needed. We just iterate over the
blocks, keep the ones whose `@type` is `Physician`, and read fields
directly. By contrast, Practo embeds quasi-JSON inside an HTML
template and requires regex extraction with bounded windows.

Per-page coverage
-----------------
Lybrate paginates ~10 doctors per page. The page header exposes
`"totalPages":N`; we don't trust it (sometimes drifts) and instead
detect saturation the same way as Practo — stop once a page yields
zero new entries. Hard ceiling via `max_pages`.

What's NOT on the listing page (and hence missing from the entry)
-----------------------------------------------------------------
- `telephone` — Lybrate exposes the clinic phone on the doctor's
  profile page, not in the listing JSON-LD.
- `clinic_name` — Lybrate's listings are doctor-centric; the clinic
  name is usually only on the profile page.

Both fields exist in our `LybrateListing` model but stay None at
discovery time. A future per-doctor enrichment step can fill them
in.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx
from loguru import logger


LYBRATE_BASE_URL = "https://www.lybrate.com"


# Lybrate uses the same city slugs as the Places API for the cities
# we care about (lowercase). Override map kept for parity with
# Practo's gateway and for future safety as we expand cities.
_CITY_OVERRIDES: dict[str, str] = {
    "bengaluru": "bangalore",
    "new delhi": "delhi",
    "calcutta": "kolkata",
    "bombay": "mumbai",
    "madras": "chennai",
    "gurugram": "gurgaon",
}


def lybrate_city_slug(city: str) -> str:
    """Normalize a city name to Lybrate's URL slug."""
    if not city:
        return ""
    norm = " ".join(city.strip().lower().split())
    if not norm:
        return ""
    return _CITY_OVERRIDES.get(norm, norm.replace(" ", "-"))


_DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


_JSONLD_PATTERN = re.compile(
    r'<script\s+type="application/ld\+json"\s*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


@dataclass(frozen=True)
class LybrateDirectoryEntry:
    """One doctor card as returned by the gateway. Discovery-time
    fields (city, discovered_at) are added by the controller."""

    profile_url: str
    doctor_name: str
    address: str | None
    locality: str | None
    postal_code: str | None
    lat: float | None
    lng: float | None
    specialty: str | None


class LybrateDirectoryGateway:
    """Paginates Lybrate's per-city dentist directory."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 30.0,
        max_pages: int = 25,
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

    def __enter__(self) -> "LybrateDirectoryGateway":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def fetch_for_city(self, city: str) -> list[LybrateDirectoryEntry]:
        """Return every dentist directory entry for `city`, deduped
        by profile URL.

        Pagination terminates on saturation (a page with zero new
        URLs); `max_pages` is the hard ceiling.

        On HTTP / parse errors: logs and returns whatever was
        collected so far. Never raises for per-page issues.
        """
        slug = lybrate_city_slug(city)
        if not slug:
            logger.warning("lybrate_directory.no_slug city={c}", c=city)
            return []

        seen_urls: set[str] = set()
        out: list[LybrateDirectoryEntry] = []

        for page in range(1, self._max_pages + 1):
            url = f"{LYBRATE_BASE_URL}/{slug}/dentist?page={page}"
            html = self._fetch_page(url)
            if html is None:
                break

            page_entries = list(_extract_entries(html, city_slug=slug))
            new_for_page = 0
            for entry in page_entries:
                if entry.profile_url in seen_urls:
                    continue
                seen_urls.add(entry.profile_url)
                out.append(entry)
                new_for_page += 1

            logger.info(
                "lybrate_directory.page city={c} page={p} found={f} new={n}",
                c=city, p=page, f=len(page_entries), n=new_for_page,
            )

            if new_for_page == 0:
                break

        logger.info(
            "lybrate_directory.done city={c} total_unique={n}",
            c=city, n=len(out),
        )
        return out

    def _fetch_page(self, url: str) -> str | None:
        try:
            resp = self._client.get(url)
        except httpx.HTTPError as e:
            logger.error(
                "lybrate_directory.fetch_error url={u} err={e}", u=url, e=e,
            )
            return None
        if resp.status_code != 200:
            logger.warning(
                "lybrate_directory.bad_status url={u} status={s}",
                u=url, s=resp.status_code,
            )
            return None
        return resp.text


def _extract_entries(
    html: str, *, city_slug: str
) -> list[LybrateDirectoryEntry]:
    """Pull every Physician JSON-LD block out of a listing page and
    map it to a `LybrateDirectoryEntry`.

    `city_slug` filters out cross-city promo entries. Lybrate's
    listing pages occasionally surface doctors whose profile URL
    targets a different city; we drop those because our discovery
    is per-city.

    Per-page deduplication isn't done here — that's the gateway's
    job (across pages too). This function returns one entry per
    Physician block, in document order.
    """
    out: list[LybrateDirectoryEntry] = []
    for match in _JSONLD_PATTERN.finditer(html):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("@type") != "Physician":
            continue

        entry = _physician_to_entry(data)
        if entry is None:
            continue
        if f"/{city_slug}/doctor/" not in entry.profile_url:
            continue
        out.append(entry)
    return out


def _physician_to_entry(data: dict) -> LybrateDirectoryEntry | None:
    profile_url = data.get("url")
    name = data.get("name")
    if not isinstance(profile_url, str) or not isinstance(name, str):
        return None
    if not profile_url.strip() or not name.strip():
        return None

    # Address: schema permits a list with one or more PostalAddress;
    # we take the first.
    addr = data.get("address")
    if isinstance(addr, list) and addr:
        addr = addr[0]
    if not isinstance(addr, dict):
        addr = {}

    geo = data.get("geo")
    if not isinstance(geo, dict):
        geo = {}

    specialty = None
    spec = data.get("medicalSpecialty")
    if isinstance(spec, dict):
        specialty = spec.get("name") if isinstance(spec.get("name"), str) else None

    return LybrateDirectoryEntry(
        profile_url=profile_url.strip(),
        doctor_name=name.strip(),
        address=_str_or_none(addr.get("streetAddress")),
        locality=_str_or_none(addr.get("addressLocality")),
        postal_code=_str_or_none(addr.get("postalCode")),
        lat=_float_or_none(geo.get("latitude")),
        lng=_float_or_none(geo.get("longitude")),
        specialty=specialty,
    )


def _str_or_none(v: object) -> str | None:
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def _float_or_none(v: object) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


__all__ = [
    "LYBRATE_BASE_URL",
    "LybrateDirectoryEntry",
    "LybrateDirectoryGateway",
    "lybrate_city_slug",
]
