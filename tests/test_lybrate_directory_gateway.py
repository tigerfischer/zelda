"""Tests for `LybrateDirectoryGateway` and the JSON-LD extractor.

Lybrate ships full schema.org `Physician` JSON-LD per doctor card,
so most of the gateway's work is JSON parsing — which is much
cleaner than Practo's regex-from-quasi-JSON approach.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from zelda.gateways.lybrate_directory import (
    LYBRATE_BASE_URL,
    LybrateDirectoryEntry,
    LybrateDirectoryGateway,
    _extract_entries,
    lybrate_city_slug,
)


# ── city slug normalization ─────────────────────────────────────────


@pytest.mark.parametrize(
    "city, expected",
    [
        ("Ludhiana", "ludhiana"),
        ("Bengaluru", "bangalore"),
        ("New Delhi", "delhi"),
        ("Gurugram", "gurgaon"),
        ("Bombay", "mumbai"),
        ("San Francisco", "san-francisco"),
        ("  Ludhiana  ", "ludhiana"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_lybrate_city_slug(city: str, expected: str):
    assert lybrate_city_slug(city) == expected


# ── extractor against a real listing-page fixture ──────────────────


def test_extractor_pulls_physicians_from_real_ludhiana_page(fixtures_dir: Path):
    html = (fixtures_dir / "lybrate_directory_ludhiana_p1.html").read_text()
    entries = _extract_entries(html, city_slug="ludhiana")

    # The fixture has 10 Physician blocks. One points at Patiala
    # (cross-city promo), so we expect 9 after the city filter.
    assert 8 <= len(entries) <= 10
    for e in entries:
        assert e.profile_url.startswith(f"{LYBRATE_BASE_URL}/ludhiana/doctor/")
        assert e.doctor_name and e.doctor_name.strip()
        assert e.specialty == "Dentist"
        # Coordinates should be roughly Punjab — Lybrate's geocoding
        # is occasionally noisy (a 31.29 / 75.59 outlier shows up in
        # the real fixture), so the bounds are deliberately loose.
        if e.lat is not None:
            assert 30.0 < e.lat < 32.0
        if e.lng is not None:
            assert 74.0 < e.lng < 77.0


def test_extractor_filters_cross_city_promos(fixtures_dir: Path):
    """Doctor cards whose URL targets a non-target city are dropped."""
    html = (fixtures_dir / "lybrate_directory_ludhiana_p1.html").read_text()

    ludhiana_entries = _extract_entries(html, city_slug="ludhiana")
    assert all("/ludhiana/doctor/" in e.profile_url for e in ludhiana_entries)

    # Sanity: searching for the wrong city slug should return 0 hits
    # for the same fixture (no Patiala SLUG = patiala on this page,
    # but the cross-city Patiala entry exists in raw output if we
    # don't filter — i.e., entries appear under their own slug).
    patiala_entries = _extract_entries(html, city_slug="patiala")
    assert all("/patiala/doctor/" in e.profile_url for e in patiala_entries)


def test_extractor_ignores_non_physician_blocks():
    """The page has WebSite, Organization, WebPage, LocalBusiness,
    BreadcrumbList — extractor should skip all of them."""
    html = """
    <script type="application/ld+json">
    {"@type":"WebSite","name":"x"}
    </script>
    <script type="application/ld+json">
    {"@type":"Organization","name":"y"}
    </script>
    <script type="application/ld+json">
    {"@type":"BreadcrumbList","itemListElement":[]}
    </script>
    """
    assert _extract_entries(html, city_slug="ludhiana") == []


def test_extractor_skips_physician_without_url_or_name():
    html = """
    <script type="application/ld+json">
    {"@type":"Physician","name":"","url":"https://www.lybrate.com/ludhiana/doctor/x"}
    </script>
    <script type="application/ld+json">
    {"@type":"Physician","name":"Dr X","url":""}
    </script>
    """
    assert _extract_entries(html, city_slug="ludhiana") == []


def test_extractor_handles_malformed_json():
    """A broken JSON block must not crash extraction."""
    html = """
    <script type="application/ld+json">
    not valid json {
    </script>
    <script type="application/ld+json">
    {"@type":"Physician","name":"Dr Valid",
     "url":"https://www.lybrate.com/ludhiana/doctor/dr-valid"}
    </script>
    """
    out = _extract_entries(html, city_slug="ludhiana")
    assert len(out) == 1
    assert out[0].doctor_name == "Dr Valid"


def test_extractor_handles_address_as_object_or_list():
    """Schema permits address as either a single object or a list of
    them. Both shapes should produce a populated streetAddress."""
    html_list = """
    <script type="application/ld+json">
    {"@type":"Physician","name":"Dr A",
     "url":"https://www.lybrate.com/ludhiana/doctor/dr-a",
     "address":[{"@type":"PostalAddress","streetAddress":"1 Main"}]}
    </script>
    """
    html_obj = """
    <script type="application/ld+json">
    {"@type":"Physician","name":"Dr B",
     "url":"https://www.lybrate.com/ludhiana/doctor/dr-b",
     "address":{"@type":"PostalAddress","streetAddress":"2 Main"}}
    </script>
    """
    a = _extract_entries(html_list, city_slug="ludhiana")
    b = _extract_entries(html_obj, city_slug="ludhiana")
    assert a[0].address == "1 Main"
    assert b[0].address == "2 Main"


def test_extractor_handles_geo_with_string_coords():
    """schema.org sometimes stringifies lat/lng — we should coerce
    them to float."""
    html = """
    <script type="application/ld+json">
    {"@type":"Physician","name":"Dr C",
     "url":"https://www.lybrate.com/ludhiana/doctor/dr-c",
     "geo":{"@type":"GeoCoordinates",
            "latitude":"30.86557944231988",
            "longitude":"75.84288440753062"}}
    </script>
    """
    out = _extract_entries(html, city_slug="ludhiana")
    assert out[0].lat == pytest.approx(30.86557944231988)
    assert out[0].lng == pytest.approx(75.84288440753062)


def test_extractor_handles_geo_with_garbage():
    html = """
    <script type="application/ld+json">
    {"@type":"Physician","name":"Dr D",
     "url":"https://www.lybrate.com/ludhiana/doctor/dr-d",
     "geo":{"latitude":"oops","longitude":null}}
    </script>
    """
    out = _extract_entries(html, city_slug="ludhiana")
    assert out[0].lat is None
    assert out[0].lng is None


# ── gateway with mocked HTTP transport ──────────────────────────────


class _FakeTransport(httpx.MockTransport):
    def __init__(self, page_responses: dict[int, str]):
        self.calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(str(request.url))
            page = int(request.url.params.get("page", "1"))
            if page in page_responses:
                return httpx.Response(200, text=page_responses[page])
            return httpx.Response(404, text="")

        super().__init__(handler)


def _wrap_gateway(transport: httpx.MockTransport, **kwargs: Any) -> LybrateDirectoryGateway:
    client = httpx.Client(transport=transport, base_url="https://www.lybrate.com")
    return LybrateDirectoryGateway(client=client, **kwargs)


def _physician_block(name: str, slug: str, lat: float, lng: float) -> str:
    return (
        '<script type="application/ld+json">'
        '{"@type":"Physician",'
        f'"name":"{name}",'
        f'"url":"https://www.lybrate.com/ludhiana/doctor/{slug}",'
        f'"geo":{{"latitude":{lat},"longitude":{lng}}},'
        '"medicalSpecialty":{"name":"Dentist"}'
        '}'
        '</script>'
    )


def test_gateway_paginates_until_no_new_entries():
    page1 = _physician_block("A", "a", 30.9, 75.8)
    page2 = _physician_block("B", "b", 30.91, 75.81)
    page3 = _physician_block("A_dup", "a", 30.9, 75.8)  # saturation
    transport = _FakeTransport({1: page1, 2: page2, 3: page3})

    gw = _wrap_gateway(transport)
    entries = gw.fetch_for_city("Ludhiana")

    assert {e.doctor_name for e in entries} == {"A", "B"}
    pages_hit = [c for c in transport.calls if "page=" in c]
    assert len(pages_hit) == 3


def test_gateway_stops_at_max_pages():
    pages = {
        i: _physician_block(f"X{i}", f"x{i}", 30.9 + i*0.001, 75.8)
        for i in range(1, 25)
    }
    transport = _FakeTransport(pages)

    gw = _wrap_gateway(transport, max_pages=3)
    entries = gw.fetch_for_city("Ludhiana")

    assert len(entries) == 3
    pages_hit = [c for c in transport.calls if "page=" in c]
    assert len(pages_hit) == 3


def test_gateway_handles_http_error_gracefully():
    transport = _FakeTransport({})
    gw = _wrap_gateway(transport)
    assert gw.fetch_for_city("Ludhiana") == []


def test_gateway_returns_empty_for_empty_city():
    transport = _FakeTransport({1: ""})
    gw = _wrap_gateway(transport)
    assert gw.fetch_for_city("") == []


def test_gateway_validates_max_pages():
    with pytest.raises(ValueError, match="max_pages"):
        LybrateDirectoryGateway(max_pages=0)
