import json
from datetime import datetime, timezone
from pathlib import Path

from zelda.models.place import Place, PlaceDetails, raw_lead_from_place_details
from zelda.models.raw_lead import RawLead


def _now() -> datetime:
    return datetime(2026, 4, 29, 10, 0, 0, tzinfo=timezone.utc)


def test_raw_lead_round_trips_through_json():
    lead = RawLead(
        place_id="ChIJ_TEST",
        city="Ludhiana",
        name="Test Dental Clinic",
        rating=4.5,
        review_count=120,
        types=["dentist", "doctor"],
        extras={"some_field": "some_value"},
        raw_json={"id": "ChIJ_TEST", "displayName": {"text": "Test Dental Clinic"}},
        discovered_at=_now(),
        last_modified_at=_now(),
    )
    dumped = lead.model_dump(mode="json")
    restored = RawLead.model_validate(dumped)

    assert restored == lead


def test_raw_lead_handles_missing_optionals():
    lead = RawLead(
        place_id="ChIJ_X",
        city="Ludhiana",
        name="Sparse Clinic",
        discovered_at=_now(),
        last_modified_at=_now(),
    )
    assert lead.phone is None
    assert lead.website is None
    assert lead.types is None
    assert lead.extras == {}
    assert lead.raw_json == {}
    assert lead.last_synced_at is None


def test_raw_lead_extras_preserves_arbitrary_nested_values():
    lead = RawLead(
        place_id="ChIJ_X",
        city="Ludhiana",
        name="Test",
        extras={"weird_field": [1, 2, {"nested": True}]},
        discovered_at=_now(),
        last_modified_at=_now(),
    )
    assert lead.extras["weird_field"] == [1, 2, {"nested": True}]


def test_place_parses_text_search_result():
    raw = {
        "id": "ChIJ_FOO",
        "displayName": {"text": "Foo Dental", "languageCode": "en"},
        "formattedAddress": "123 Main St, Ludhiana",
    }
    place = Place.model_validate(raw)

    assert place.id == "ChIJ_FOO"
    assert place.display_name.text == "Foo Dental"
    assert place.formatted_address == "123 Main St, Ludhiana"


def test_place_details_parses_full_response(fixtures_dir: Path):
    raw = json.loads((fixtures_dir / "place_details_sample.json").read_text())
    details = PlaceDetails.model_validate(raw)

    assert details.id == raw["id"]
    assert details.display_name.text == raw["displayName"]["text"]
    assert details.rating == raw["rating"]
    assert details.user_rating_count == raw["userRatingCount"]
    assert details.location is not None
    assert details.location.latitude == raw["location"]["latitude"]
    assert details.reviews is not None
    assert len(details.reviews) == len(raw["reviews"])
    assert details.editorial_summary is not None
    assert details.editorial_summary.text == raw["editorialSummary"]["text"]


def test_raw_lead_from_place_details_converter(fixtures_dir: Path):
    raw = json.loads((fixtures_dir / "place_details_sample.json").read_text())
    lead = raw_lead_from_place_details(raw, city="Ludhiana", now=_now())

    assert lead.place_id == raw["id"]
    assert lead.city == "Ludhiana"
    assert lead.name == raw["displayName"]["text"]
    assert lead.formatted_address == raw["formattedAddress"]
    assert lead.short_address == raw["shortFormattedAddress"]
    assert lead.lat == raw["location"]["latitude"]
    assert lead.lng == raw["location"]["longitude"]
    assert lead.phone == raw["nationalPhoneNumber"]
    assert lead.phone_intl == raw["internationalPhoneNumber"]
    assert lead.website == raw["websiteUri"]
    assert lead.google_maps_url == raw["googleMapsUri"]
    assert lead.rating == raw["rating"]
    assert lead.review_count == raw["userRatingCount"]
    assert lead.business_status == raw["businessStatus"]
    assert lead.primary_type == raw["primaryType"]
    assert lead.types == raw["types"]
    assert lead.price_level == raw["priceLevel"]
    assert lead.editorial_summary == raw["editorialSummary"]["text"]
    assert lead.photos_count == len(raw["photos"])
    assert lead.reviews is not None
    assert len(lead.reviews) == len(raw["reviews"])
    assert lead.opening_hours is not None
    assert lead.discovered_at == _now()
    assert lead.last_modified_at == _now()


def test_converter_preserves_unknown_fields_in_raw_json(fixtures_dir: Path):
    """Even if our PlaceDetails model doesn't know a field, raw_json must
    keep it — we never want to drop information from the API response."""
    raw = json.loads((fixtures_dir / "place_details_sample.json").read_text())
    assert "_unknownFutureField" in raw, "fixture should include an unknown field"

    lead = raw_lead_from_place_details(raw, city="Ludhiana")

    assert lead.raw_json == raw
    assert lead.raw_json["_unknownFutureField"] == raw["_unknownFutureField"]


def test_real_ludhiana_response_parses(fixtures_dir: Path):
    """Drift detector: if the live Places API changes shape and our model
    can no longer parse it, this test fires immediately. The fixture is
    captured by `scripts/smoke_places.py`."""
    import pytest

    path = fixtures_dir / "place_details_real_ludhiana.json"
    if not path.exists():
        pytest.skip(f"Real fixture not captured yet — run scripts/smoke_places.py")

    raw = json.loads(path.read_text())
    details = PlaceDetails.model_validate(raw)
    lead = raw_lead_from_place_details(raw, city="Ludhiana")

    assert details.id == raw["id"]
    assert lead.place_id == raw["id"]
    assert lead.city == "Ludhiana"
    assert lead.raw_json == raw
    # Optional fields may be None in real data — that's fine, but the
    # ones the API actually returned should make it onto the lead.
    if "rating" in raw:
        assert lead.rating == raw["rating"]
    if "userRatingCount" in raw:
        assert lead.review_count == raw["userRatingCount"]
