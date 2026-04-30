"""Tests for the Playwright-backed Practo search gateway.

Covers the pure helpers (URL building, profile-URL normalization,
SERP-state parsing) and round-trips a real captured Redux state.
The Playwright orchestration is smoke-tested live (see
scripts/smoke_practo_search.py).
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from zelda.gateways.practo_search import (
    PractoSearchResult,
    build_search_url,
    normalize_profile_url,
    parse_search_state,
    _entity_to_result,
)


_T = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
_FIXTURES = Path(__file__).parent / "fixtures"


# ── build_search_url ────────────────────────────────────────────────


def test_build_search_url_assembles_correct_endpoint():
    url = build_search_url(query="Sai Dental Clinic", city_slug="ludhiana")
    parts = urlsplit(url)
    assert parts.scheme == "https"
    assert parts.netloc == "www.practo.com"
    assert parts.path == "/search/doctors"

    qs = parse_qs(parts.query)
    assert qs["city"] == ["Ludhiana"]  # title-cased from slug
    # `q` is a JSON-encoded list with the query payload
    q_payload = json.loads(qs["q"][0])
    assert q_payload == [
        {"word": "Sai Dental Clinic", "autocompleted": False, "category": "doctor"}
    ]


def test_build_search_url_handles_special_chars_in_query():
    """rapidfuzz can score unicode names, so the URL must transmit them
    cleanly. urlencode handles % escaping; `json.dumps` keeps unicode
    as escaped \\u sequences which is fine for transport."""
    url = build_search_url(query="Dr. K & A Clinic", city_slug="bangalore")
    qs = parse_qs(urlsplit(url).query)
    assert qs["city"] == ["Bangalore"]
    payload = json.loads(qs["q"][0])
    assert payload[0]["word"] == "Dr. K & A Clinic"


def test_build_search_url_strips_whitespace_in_slug():
    url = build_search_url(query="X", city_slug="  ludhiana  ")
    assert "city=Ludhiana" in url


# ── normalize_profile_url ──────────────────────────────────────────


def test_normalize_profile_url_keeps_only_practice_id():
    """Practo embeds the search query into the SERP-emitted profile_url
    via `specialization=`, `referrer=`, `page_uid=`. Strip them so the
    stored URL is canonical."""
    raw = (
        "/ludhiana/doctor/neeraj-kumar-goyal-urologist"
        "?practice_id=927413&specialization=Sai%20Dental&referrer=doctor_listing"
        "&page_uid=ddc35867-881d-45aa"
    )
    out = normalize_profile_url(raw)
    parts = urlsplit(out)
    assert parts.scheme == "https"
    assert parts.netloc == "www.practo.com"
    assert parts.path == "/ludhiana/doctor/neeraj-kumar-goyal-urologist"
    qs = parse_qs(parts.query)
    assert qs == {"practice_id": ["927413"]}


def test_normalize_profile_url_handles_absolute_url():
    raw = "https://www.practo.com/ludhiana/doctor/x?practice_id=1&referrer=foo"
    out = normalize_profile_url(raw)
    assert out == "https://www.practo.com/ludhiana/doctor/x?practice_id=1"


def test_normalize_profile_url_handles_no_query():
    raw = "/ludhiana/doctor/x"
    out = normalize_profile_url(raw)
    assert out == "https://www.practo.com/ludhiana/doctor/x"


def test_normalize_profile_url_returns_empty_for_garbage_input():
    assert normalize_profile_url("") == ""
    assert normalize_profile_url("   ") == ""
    # Garbage but parseable into a URL — best-effort returns it normalized.
    assert normalize_profile_url(None) == ""  # type: ignore[arg-type]


# ── _entity_to_result ──────────────────────────────────────────────


def _full_entity(**overrides) -> dict:
    """Minimum-viable SERP entity dict the parser accepts."""
    base = {
        "doctor_name": "Dr. X",
        "clinic_name": "X Clinic",
        "specialization": "Dentist",
        "locality": "Locality1",
        "image_url": "https://img/x.jpg",
        "profile_url": "/ludhiana/doctor/dr-x?practice_id=42&referrer=foo",
        "is_practo_prime": False,
        "is_practo_prime_basic": False,
        "is_practo_prime_online": False,
        "is_prime_badge_enabled": False,
        "practice": {"name": "X Clinic", "locality": "Locality1"},
    }
    base.update(overrides)
    return base


def test_entity_to_result_happy_path():
    out = _entity_to_result(_full_entity())
    assert isinstance(out, PractoSearchResult)
    assert out.practo_url == "https://www.practo.com/ludhiana/doctor/dr-x?practice_id=42"
    assert out.doctor_name == "Dr. X"
    assert out.clinic_name == "X Clinic"
    assert out.specialization == "Dentist"
    assert out.locality == "Locality1"
    assert out.profile_image_url == "https://img/x.jpg"
    assert out.verified_badge is False
    # raw preserves the source dict.
    assert out.raw["doctor_name"] == "Dr. X"


def test_entity_to_result_returns_none_when_profile_url_missing():
    ent = _full_entity()
    ent.pop("profile_url")
    assert _entity_to_result(ent) is None


def test_entity_to_result_falls_back_to_practice_name_for_clinic():
    ent = _full_entity()
    ent.pop("clinic_name")
    out = _entity_to_result(ent)
    assert out is not None
    assert out.clinic_name == "X Clinic"  # from practice.name


def test_entity_to_result_verified_badge_on_any_paid_tier():
    for flag in (
        "is_practo_prime",
        "is_practo_prime_basic",
        "is_practo_prime_online",
        "is_prime_badge_enabled",
    ):
        ent = _full_entity(**{flag: True})
        out = _entity_to_result(ent)
        assert out is not None
        assert out.verified_badge is True, f"flag {flag} should mark verified"


def test_entity_to_result_falls_back_to_profile_photo_url():
    ent = _full_entity()
    ent.pop("image_url")
    ent["profile_photo"] = {"url": "https://img/photo.jpg"}
    out = _entity_to_result(ent)
    assert out is not None
    assert out.profile_image_url == "https://img/photo.jpg"


# ── parse_search_state ─────────────────────────────────────────────


def test_parse_search_state_handles_empty_redux():
    assert parse_search_state({}) == []
    assert parse_search_state({"listingV2": {}}) == []
    assert parse_search_state({"listingV2": {"doctors": {}}}) == []


def test_parse_search_state_walks_items_in_order():
    redux = {
        "listingV2": {
            "doctors": {
                "items": [{"id": 2}, {"id": 1}, {"id": 3}],
                "entities": {
                    "1": _full_entity(
                        doctor_name="Dr. One",
                        profile_url="/ludhiana/doctor/dr-one?practice_id=1",
                    ),
                    "2": _full_entity(
                        doctor_name="Dr. Two",
                        profile_url="/ludhiana/doctor/dr-two?practice_id=2",
                    ),
                    "3": _full_entity(
                        doctor_name="Dr. Three",
                        profile_url="/ludhiana/doctor/dr-three?practice_id=3",
                    ),
                },
            }
        }
    }
    out = parse_search_state(redux)
    assert [r.doctor_name for r in out] == ["Dr. Two", "Dr. One", "Dr. Three"]


def test_parse_search_state_respects_max_results():
    redux = {
        "listingV2": {
            "doctors": {
                "items": [{"id": i} for i in range(5)],
                "entities": {
                    str(i): _full_entity(
                        doctor_name=f"Dr. {i}",
                        profile_url=f"/ludhiana/doctor/dr-{i}?practice_id={i}",
                    )
                    for i in range(5)
                },
            }
        }
    }
    out = parse_search_state(redux, max_results=2)
    assert len(out) == 2


def test_parse_search_state_skips_unparseable_entities():
    redux = {
        "listingV2": {
            "doctors": {
                "items": [{"id": "missing"}, {"id": "good"}],
                "entities": {
                    "good": _full_entity(doctor_name="Dr. Good"),
                    # "missing" intentionally absent
                },
            }
        }
    }
    out = parse_search_state(redux)
    assert [r.doctor_name for r in out] == ["Dr. Good"]


# ── real fixture round-trip ────────────────────────────────────────


def test_parse_search_state_real_fixture():
    """End-to-end against a real Practo SERP capture (Ludhiana 2026-04-30).
    Schema-drift canary."""
    with (_FIXTURES / "practo_search_redux_sample.json").open() as f:
        redux = json.load(f)

    out = parse_search_state(redux, max_results=10)

    # The fixture has 10 candidates.
    assert len(out) == 10

    # Every result has a non-empty profile URL pointing at practo.com.
    # Practo segments paths by speciality (`/doctor/`, `/therapist/`,
    # `/dietitian/`, ...) — the parser preserves all of them and lets
    # the controller's fuzzy match decide what's relevant.
    for r in out:
        assert r.practo_url.startswith("https://www.practo.com/")
        assert r.doctor_name  # all candidates have a name on the SERP
    # Specialization is best-effort: Practo occasionally returns it
    # null on the SERP entity, which is fine because the discovery
    # controller scores on names, not roles.
    n_with_spec = sum(1 for r in out if r.specialization)
    assert n_with_spec >= len(out) - 2, (
        f"expected most candidates to have specialization, got {n_with_spec}/{len(out)}"
    )

    # The fixture is a "Sai Dental" Ludhiana search; Practo padded
    # the SERP with non-matches (urologists, dietitians, etc.) since
    # there's no exact dentist match. We don't pin a specific top
    # result — Practo's relevance ranking can shift.

    # URL canonicalization stripped the search-context leak on every
    # candidate: the SERP-emitted URL had specialization=/referrer=/
    # page_uid= params, all of which should be gone.
    for r in out:
        assert "specialization=" not in r.practo_url
        assert "referrer=" not in r.practo_url
        assert "page_uid=" not in r.practo_url
    # Some entities have practice_id (most do), others might not.
    n_with_practice_id = sum(1 for r in out if "practice_id=" in r.practo_url)
    assert n_with_practice_id >= 7, (
        f"expected most candidates to carry practice_id, got {n_with_practice_id}/10"
    )
