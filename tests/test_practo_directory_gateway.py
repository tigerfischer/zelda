"""Tests for `PractoDirectoryGateway` and the JSON-LD card extractor.

Two layers under test:
- `_extract_entries`: pure function, runs against a real Practo
  Ludhiana fixture + synthetic edge-case inputs.
- `PractoDirectoryGateway.fetch_for_city`: pagination + dedup +
  saturation detection, run against a mocked HTTP transport.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from zelda.gateways.practo_directory import (
    PRACTO_BASE_URL,
    PractoDirectoryEntry,
    PractoDirectoryGateway,
    _extract_entries,
    practo_city_slug,
)


# ── city slug normalization ─────────────────────────────────────────


@pytest.mark.parametrize(
    "city, expected",
    [
        ("Ludhiana", "ludhiana"),
        ("Bengaluru", "bangalore"),
        ("New Delhi", "delhi"),
        ("Gurugram", "gurgaon"),
        ("Calcutta", "kolkata"),
        ("Bombay", "mumbai"),
        ("Madras", "chennai"),
        ("San Francisco", "san-francisco"),
        ("  Ludhiana  ", "ludhiana"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_practo_city_slug(city: str, expected: str):
    assert practo_city_slug(city) == expected


# ── card extractor against a real listing-page fixture ──────────────


def test_extractor_pulls_clinics_from_real_ludhiana_page(fixtures_dir: Path):
    html = (fixtures_dir / "practo_directory_ludhiana_p1.html").read_text()
    entries = _extract_entries(html, city_slug="ludhiana")

    # Page 1 has ~10 cards; we accept >=8 to allow for a couple
    # missing JSON-LD payloads.
    assert len(entries) >= 8
    for e in entries:
        assert e.profile_url.startswith(f"{PRACTO_BASE_URL}/ludhiana/clinic/")
        assert e.name and e.name.strip()
        assert e.lat is not None and e.lng is not None
        # Ludhiana sits around (30.9, 75.85). All entries should be near.
        assert 30.5 < e.lat < 31.2
        assert 75.4 < e.lng < 76.3


def test_extractor_filters_cross_city_cards():
    """A card whose URL targets a different city slug should be
    dropped — Practo occasionally injects cross-city promo cards."""
    fake_html = """
    "name":"Cross-City Decoy"
    "streetAddress":"99, Some Place"
    "latitude":12.34
    "longitude":56.78
    "url":"https://www.practo.com/mumbai/clinic/decoy-bandra"
    """
    assert _extract_entries(fake_html, city_slug="ludhiana") == []


def test_extractor_ignores_card_without_url():
    fake_html = """
    "name":"No URL Clinic"
    "streetAddress":"some address"
    "latitude":30.9
    "longitude":75.8
    """
    assert _extract_entries(fake_html, city_slug="ludhiana") == []


def test_extractor_ignores_malformed_coords():
    fake_html = """
    "name":"Bad Coords"
    "streetAddress":"x"
    "latitude":not-a-number
    "longitude":nope
    "url":"https://www.practo.com/ludhiana/clinic/bad-coords"
    """
    assert _extract_entries(fake_html, city_slug="ludhiana") == []


# ── gateway with mocked HTTP transport ──────────────────────────────


class _FakeTransport(httpx.MockTransport):
    """Records every request URL so we can assert pagination."""

    def __init__(self, page_responses: dict[int, str]):
        self.calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(str(request.url))
            page = int(request.url.params.get("page", "1"))
            if page in page_responses:
                return httpx.Response(200, text=page_responses[page])
            return httpx.Response(404, text="")

        super().__init__(handler)


def _wrap_gateway(transport: httpx.MockTransport, **kwargs: Any) -> PractoDirectoryGateway:
    client = httpx.Client(transport=transport, base_url="https://www.practo.com")
    return PractoDirectoryGateway(client=client, **kwargs)


def _card(name: str, slug: str, lat: float, lng: float) -> str:
    return (
        f'"name":"{name}",'
        f'"streetAddress":"some addr",'
        f'"latitude":{lat},"longitude":{lng},'
        f'"url":"https://www.practo.com/ludhiana/clinic/{slug}"'
    )


def test_gateway_paginates_until_no_new_entries():
    page1 = "{" + _card("A", "a", 30.9, 75.8) + "}"
    page2 = "{" + _card("B", "b", 30.91, 75.81) + "}"
    page3 = "{" + _card("A", "a", 30.9, 75.8) + "}"  # saturation: same as p1
    transport = _FakeTransport({1: page1, 2: page2, 3: page3})

    gw = _wrap_gateway(transport)
    entries = gw.fetch_for_city("Ludhiana")

    assert {e.name for e in entries} == {"A", "B"}
    pages_hit = [c for c in transport.calls if "page=" in c]
    # Loop reads page 1, 2, 3 (page 3 saturates) and stops.
    assert len(pages_hit) == 3


def test_gateway_stops_at_max_pages():
    pages = {
        i: "{" + _card(f"X{i}", f"x{i}", 30.9 + i*0.001, 75.8) + "}"
        for i in range(1, 25)
    }
    transport = _FakeTransport(pages)

    gw = _wrap_gateway(transport, max_pages=3)
    entries = gw.fetch_for_city("Ludhiana")

    assert len(entries) == 3
    pages_hit = [c for c in transport.calls if "page=" in c]
    assert len(pages_hit) == 3


def test_gateway_handles_http_error_gracefully():
    transport = _FakeTransport({})  # every fetch returns 404
    gw = _wrap_gateway(transport)
    assert gw.fetch_for_city("Ludhiana") == []


def test_gateway_returns_empty_for_empty_city():
    transport = _FakeTransport({1: "{}"})
    gw = _wrap_gateway(transport)
    assert gw.fetch_for_city("") == []


def test_gateway_dedupes_across_pages_by_url():
    """Same profile URL appearing on pages 1 and 2 should yield one
    entry. Page 2 must also introduce SOMETHING new, otherwise the
    saturation check stops the loop before later pages run."""
    page1 = "{" + _card("A", "a", 30.9, 75.8) + "}"
    # Page 2: re-emits A (dedup — drop) and adds B.
    page2 = (
        "{" + _card("A_again", "a", 30.91, 75.81) + "}"
        "{" + _card("B", "b", 30.92, 75.82) + "}"
    )
    page3 = "{" + _card("A_dup3", "a", 30.9, 75.8) + "}"  # saturation
    transport = _FakeTransport({1: page1, 2: page2, 3: page3})

    gw = _wrap_gateway(transport)
    entries = gw.fetch_for_city("Ludhiana")

    urls = [e.profile_url for e in entries]
    # /clinic/a from page 1, /clinic/b from page 2, no duplicates.
    assert urls == [
        "https://www.practo.com/ludhiana/clinic/a",
        "https://www.practo.com/ludhiana/clinic/b",
    ]


def test_gateway_validates_max_pages():
    with pytest.raises(ValueError, match="max_pages"):
        PractoDirectoryGateway(max_pages=0)
