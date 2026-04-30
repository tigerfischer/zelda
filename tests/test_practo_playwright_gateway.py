"""Tests for the Playwright-backed Practo gateway.

The Playwright orchestration is smoke-tested live (see
scripts/smoke_practo.py); this file covers the *pure* parser and
helpers, plus a fixture round-trip from a real captured Practo
__REDUX_STATE__ + JSON-LD pair.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from zelda.gateways.practo_playwright import (
    parse_practo_state,
    _is_challenge_page,
    _extract_specializations,
    _extract_membership_names,
    _join_address_lines,
    _parse_iso_datetime,
    _find_jsonld,
    _first_int,
    _first_float,
    _first_str,
    _to_str_list,
    _clean_dict_list,
)


_T = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
_FIXTURES = Path(__file__).parent / "fixtures"


# ── pure helpers ────────────────────────────────────────────────────


def test_first_str_picks_first_non_empty():
    assert _first_str({"a": "X"}, "a", "b") == "X"
    assert _first_str({"a": "  hello  "}, "a") == "hello"
    assert _first_str({"a": ""}, "a", "b") is None


def test_first_int_handles_int_float_and_string():
    assert _first_int({"a": 57}, "a") == 57
    assert _first_int({"a": 57.9}, "a") == 57
    assert _first_int({"a": "57 years"}, "a") == 57
    assert _first_int({"a": "1,234"}, "a") == 1234
    assert _first_int({"a": "no digits"}, "a") is None


def test_first_int_skips_bools():
    assert _first_int({"a": True, "b": 5}, "a", "b") == 5


def test_first_float_handles_strings_and_numbers():
    assert _first_float({"r": 4.2}, "r") == 4.2
    assert _first_float({"r": "4.2"}, "r") == 4.2
    assert _first_float({"r": "not a number"}, "r") is None


def test_to_str_list_handles_lists_dicts_strings_and_none():
    assert _to_str_list(["A", " B "]) == ["A", "B"]
    assert _to_str_list([{"name": "X"}, {"title": "Y"}]) == ["X", "Y"]
    assert _to_str_list("English, Hindi") == ["English", "Hindi"]
    assert _to_str_list(None) == []
    assert _to_str_list(42) == []


def test_clean_dict_list_keeps_only_dicts():
    assert _clean_dict_list([{"a": 1}, "junk", None, {"b": 2}]) == [
        {"a": 1}, {"b": 2}
    ]
    assert _clean_dict_list(None) == []


def test_parse_iso_datetime_handles_practo_format():
    """Practo emits e.g. '2026-04-27T04:30:00.000+0000' (no colon in
    offset); fromisoformat needs '+00:00'."""
    out = _parse_iso_datetime("2026-04-27T04:30:00.000+0000")
    assert out is not None
    assert out.year == 2026 and out.month == 4 and out.day == 27
    assert out.hour == 4 and out.minute == 30


def test_parse_iso_datetime_returns_none_for_bad_input():
    assert _parse_iso_datetime(None) is None
    assert _parse_iso_datetime("") is None
    assert _parse_iso_datetime("not a date") is None


def test_join_address_lines_combines_practo_keys():
    addr = {"address_line1": "111 Main", "address_line2": "Suite 5"}
    assert _join_address_lines(addr) == "111 Main, Suite 5"


def test_join_address_lines_handles_apify_keys():
    addr = {"line1": "111 Main", "line2": "Suite 5"}
    assert _join_address_lines(addr) == "111 Main, Suite 5"


def test_join_address_lines_skips_blank_line2():
    addr = {"address_line1": "111 Main", "address_line2": None}
    assert _join_address_lines(addr) == "111 Main"


def test_join_address_lines_returns_none_when_empty():
    assert _join_address_lines({}) is None
    assert _join_address_lines({"address_line1": "  "}) is None


# ── challenge detection ────────────────────────────────────────────


def test_is_challenge_page_by_title():
    assert _is_challenge_page(title="Challenge Validation", html="anything") is True


def test_is_challenge_page_by_body():
    assert _is_challenge_page(title="", html="<html>Challenge Validation</html>") is True


def test_is_challenge_page_clean_returns_false():
    assert _is_challenge_page(
        title="Dr. K A Mohan - Orthodontist", html="<html>real content</html>"
    ) is False


# ── specialization + membership extraction ────────────────────────


def test_extract_specializations_from_redux():
    prof = {
        "specializations": [
            {
                "subspeciality": {"sub_speciality_name": "Orthodontist"},
                "master_specialization": {
                    "speciality": {"speciality_name": "Dentist"}
                },
            },
            {
                "subspeciality": {"sub_speciality_name": "Dental Surgeon"},
            },
        ]
    }
    out = _extract_specializations(prof, jsonld=None)
    assert out == ["Orthodontist", "Dental Surgeon"]


def test_extract_specializations_fallback_to_jsonld():
    prof = {"specializations": []}
    jsonld = {"medicalSpecialty": "Dentist, Orthodontist"}
    assert _extract_specializations(prof, jsonld=jsonld) == ["Dentist", "Orthodontist"]


def test_extract_membership_names_pulls_council_names():
    raw = [
        {"council": {"name": "Karnataka State Dental Council"}},
        {"council": {"name": "FOGSI"}},
        {"name": "Direct Name"},
    ]
    assert _extract_membership_names(raw) == [
        "Karnataka State Dental Council",
        "FOGSI",
        "Direct Name",
    ]


def test_find_jsonld_picks_first_matching_type():
    blocks = [
        {"@type": "BreadcrumbList"},
        {"@type": "Dentist", "name": "Dr. X"},
    ]
    out = _find_jsonld(blocks, "Dentist", "Physician")
    assert out and out["name"] == "Dr. X"


def test_find_jsonld_walks_into_lists():
    blocks = [[{"@type": "Dentist", "name": "Dr. Y"}]]
    out = _find_jsonld(blocks, "Dentist")
    assert out and out["name"] == "Dr. Y"


# ── parse_practo_state on synthetic input ─────────────────────────


def _make_minimal_state(**overrides) -> dict:
    """Smallest Redux state shape that exercises the happy path."""
    base = {
        "profile_reducer": {
            "full_name": "Dr. Test",
            "fabric_id": "999",
            "profile_url": "https://www.practo.com/x/doctor/dr-test",
            "image_url": "https://img.example/dr-test.jpg",
            "qualifications": [
                {"master_qualification": {"name": "BDS"}, "completion_year": 2010},
            ],
            "awards": [],
            "memberships": [],
            "registrations": [],
            "services": ["Cleaning", "Braces"],
            "specializations": [
                {"subspeciality": {"sub_speciality_name": "Dentist"}},
            ],
            "external_data": {
                "recommendation": {
                    "recommendation_percent": 90,
                    "response_count": 50,
                },
            },
            "seo_data": {"description": "A dentist."},
            "relations": [{
                "is_prime_doctor": True,
                "fees": [{"type": "CONSULTATION", "amount": 600}],
                "establishment": {
                    "name": "Test Clinic",
                    "address": {
                        "address_line1": "1 Main St",
                        "address_line2": None,
                        "locality": {"name": "Locality1"},
                        "city": {"city_name": "Bangalore"},
                        "country": {"currency": "INR"},
                        "latitude": 12.5,
                        "longitude": 77.5,
                    },
                    "photos": [
                        {"url": "https://img.example/clinic1.jpg"},
                    ],
                },
                "establishment_rating": {
                    "clinic_rating": 5,
                    "total_recommendations": 365,
                },
                "availability_info": {
                    "next_available_timestamp": "2026-04-27T04:30:00.000+0000",
                },
                "timings": [
                    {"begin_time": "10:00", "end_time": "13:00",
                     "available_days": ["MONDAY"]},
                ],
            }],
        },
    }
    base["profile_reducer"].update(overrides)
    return base


def test_parse_practo_state_minimal_happy_path():
    state = _make_minimal_state()
    p = parse_practo_state(
        state, jsonld_blocks=[], place_id="ChIJ_X",
        practo_url="https://input.example", fetched_at=_T,
    )
    assert p.name == "Dr. Test"
    assert p.practo_doctor_id == "999"
    assert p.profile_url.endswith("dr-test")
    assert p.qualifications == ["BDS"]
    assert len(p.education) == 1
    assert p.specializations == ["Dentist"]
    assert p.consultation_fee == 600
    assert p.consultation_fee_currency == "INR"
    assert p.recommendation_percent == 90
    assert p.patient_count == 50
    assert p.has_practo_plus_badge is True
    assert p.next_available_at is not None
    assert p.next_available_at.year == 2026
    assert p.clinic_name == "Test Clinic"
    assert p.clinic_address == "1 Main St"
    assert p.clinic_locality == "Locality1"
    assert p.clinic_city == "Bangalore"
    assert p.lat == 12.5
    assert p.lng == 77.5
    assert p.rating == 5.0
    assert p.summary == "A dentist."
    assert p.profile_image_url == "https://img.example/dr-test.jpg"
    assert p.photo_urls == ["https://img.example/clinic1.jpg"]
    assert isinstance(p.operating_hours, list)
    assert p.fetch_status == "ok"
    assert p.fetched_at == _T


def test_parse_practo_state_handles_missing_relations():
    state = {"profile_reducer": {"full_name": "Dr. NoRel", "relations": []}}
    p = parse_practo_state(
        state, place_id="ChIJ_X", practo_url="https://x", fetched_at=_T
    )
    assert p.name == "Dr. NoRel"
    assert p.consultation_fee is None
    assert p.clinic_name is None
    assert p.has_practo_plus_badge is None  # field absent → unknown, not False


def test_parse_practo_state_prime_field_absent_means_unknown():
    state = _make_minimal_state()
    state["profile_reducer"]["relations"][0].pop("is_prime_doctor")
    p = parse_practo_state(
        state, place_id="ChIJ_X", practo_url="https://x", fetched_at=_T
    )
    assert p.has_practo_plus_badge is None


def test_parse_practo_state_prime_false_is_persisted():
    state = _make_minimal_state()
    state["profile_reducer"]["relations"][0]["is_prime_doctor"] = False
    p = parse_practo_state(
        state, place_id="ChIJ_X", practo_url="https://x", fetched_at=_T
    )
    assert p.has_practo_plus_badge is False


def test_parse_practo_state_empty_redux_returns_pending_shape():
    """Edge case: empty/garbage state. Don't crash; return mostly-None
    profile (caller decides what status to assign — gateway uses
    not_found for this case)."""
    p = parse_practo_state(
        {}, place_id="ChIJ_X", practo_url="https://x", fetched_at=_T
    )
    assert p.fetch_status == "ok"  # parser is content-agnostic
    assert p.name is None
    assert p.qualifications == []


# ── real captured fixture round-trip ──────────────────────────────


def test_parse_practo_state_real_fixture():
    """End-to-end against a real captured Practo profile (Dr. K A
    Mohan, Bangalore, captured 2026-04-29). Schema-drift canary."""
    with (_FIXTURES / "practo_redux_sample.json").open() as f:
        state = json.load(f)
    with (_FIXTURES / "practo_jsonld_sample.json").open() as f:
        jsonld = json.load(f)

    p = parse_practo_state(
        state, jsonld_blocks=jsonld, place_id="ChIJ_TEST",
        practo_url="https://input.example", fetched_at=_T,
    )

    # Identity
    assert p.name == "Dr. K A Mohan"
    assert p.practo_doctor_id == "258986"
    assert p.profile_url == "https://www.practo.com/bangalore/doctor/dr-k-a-mohan"

    # Credentials
    assert "BDS" in p.qualifications
    assert len(p.education) >= 1
    assert any(
        ((q.get("master_college") or {}).get("name", "").strip())
        for q in p.education
    )
    assert len(p.awards) == 3
    assert "Karnataka State Dental Council" in p.memberships

    # Practice
    assert p.clinic_name == "Dental De Care"
    assert p.clinic_locality == "Domlur"
    assert p.clinic_city == "Bangalore"
    assert p.clinic_address and "111" in p.clinic_address
    assert p.lat is not None and 12.0 < p.lat < 14.0
    assert p.lng is not None and 77.0 < p.lng < 78.0

    # Fees
    assert p.consultation_fee == 500
    assert p.consultation_fee_currency == "INR"

    # Reputation
    assert p.recommendation_percent == 94
    assert p.patient_count == 78
    # `reviews_count` falls back to total_recommendations on the
    # establishment when external_data.response_count is set; we use
    # response_count as the primary feedback count.
    assert p.reviews_count == 78
    assert p.rating == 5.0

    # Agency-engagement (E4)
    assert p.has_practo_plus_badge in (True, False, None)
    # Practo Plus signal exists on this fixture (field present).
    assert p.has_practo_plus_badge is not None

    # Slot availability (B5)
    assert p.next_available_at is not None
    assert p.next_available_at.year == 2026

    # Services + photos
    assert len(p.services) > 100  # 217 in this profile
    assert len(p.photo_urls) >= 1

    # Bio
    assert p.summary and len(p.summary) > 50

    # Operating hours preserved as list
    assert isinstance(p.operating_hours, list)
    assert len(p.operating_hours) >= 1

    # raw_json carries the full state for unknown-field recovery.
    assert "profile_reducer" in p.raw_json
