import json
from pathlib import Path

import httpx
import pytest

from zelda.gateways.google_places import GooglePlacesError, GooglePlacesGateway
from zelda.models.place import Place


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def gateway() -> GooglePlacesGateway:
    """A gateway tuned for fast tests: no page delay, no retry backoff."""
    return GooglePlacesGateway(
        api_key="test-api-key",
        client=httpx.Client(timeout=5.0),
        page_delay_seconds=0,
        backoff_seconds=0,
        max_attempts=3,
    )


# ── helpers ──────────────────────────────────────────────────────────────


_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"


def _details_url(place_id: str) -> str:
    return f"https://places.googleapis.com/v1/places/{place_id}"


def _ts_response(places: list[dict], next_page_token: str | None = None) -> dict:
    out: dict = {"places": places}
    if next_page_token:
        out["nextPageToken"] = next_page_token
    return out


def _mk_place(id_: str, name: str = "Test") -> dict:
    return {
        "id": id_,
        "displayName": {"text": name, "languageCode": "en"},
        "formattedAddress": "test address",
    }


# ── constructor ──────────────────────────────────────────────────────────


def test_constructor_rejects_blank_api_key():
    with pytest.raises(ValueError, match="api_key"):
        GooglePlacesGateway(api_key="")
    with pytest.raises(ValueError, match="api_key"):
        GooglePlacesGateway(api_key="   ")


def test_constructor_rejects_invalid_max_attempts():
    with pytest.raises(ValueError, match="max_attempts"):
        GooglePlacesGateway(api_key="k", max_attempts=0)


# ── text_search: parsing + pagination ────────────────────────────────────


def test_text_search_parses_response(gateway, respx_mock):
    payload = _ts_response(
        [_mk_place("ChIJ_1", "Foo Dental"), _mk_place("ChIJ_2", "Bar Clinic")]
    )
    respx_mock.post(_TEXT_SEARCH_URL).respond(200, json=payload)

    results = gateway.text_search("dentist in Ludhiana")

    assert len(results) == 2
    assert all(isinstance(r, Place) for r in results)
    assert results[0].id == "ChIJ_1"
    assert results[0].display_name.text == "Foo Dental"
    assert results[1].id == "ChIJ_2"


def test_text_search_returns_empty_when_no_results(gateway, respx_mock):
    respx_mock.post(_TEXT_SEARCH_URL).respond(200, json={"places": []})
    assert gateway.text_search("nonsense query") == []


def test_text_search_paginates_until_max_pages(gateway, respx_mock):
    page1 = _ts_response([_mk_place("ChIJ_p1_1")], next_page_token="tok1")
    page2 = _ts_response(
        [_mk_place("ChIJ_p2_1"), _mk_place("ChIJ_p2_2")], next_page_token="tok2"
    )
    page3 = _ts_response([_mk_place("ChIJ_p3_1")])

    route = respx_mock.post(_TEXT_SEARCH_URL)
    route.side_effect = [
        httpx.Response(200, json=page1),
        httpx.Response(200, json=page2),
        httpx.Response(200, json=page3),
    ]

    results = gateway.text_search("dentist", max_pages=2)

    assert len(results) == 3  # page 1 (1) + page 2 (2) only
    assert route.call_count == 2  # third page never fetched


def test_text_search_respects_max_pages_one(gateway, respx_mock):
    """The budget control mechanism: max_pages=1 means exactly one HTTP call,
    even if the response advertises more pages via nextPageToken."""
    payload = _ts_response([_mk_place("ChIJ_1")], next_page_token="tok1")
    route = respx_mock.post(_TEXT_SEARCH_URL).respond(200, json=payload)

    results = gateway.text_search("dentist", max_pages=1)

    assert len(results) == 1
    assert route.call_count == 1


def test_text_search_stops_when_no_next_token(gateway, respx_mock):
    """If the response has no nextPageToken, no further page is fetched
    even if max_pages allows it."""
    payload = _ts_response([_mk_place("ChIJ_1")])
    route = respx_mock.post(_TEXT_SEARCH_URL).respond(200, json=payload)

    results = gateway.text_search("dentist", max_pages=3)

    assert len(results) == 1
    assert route.call_count == 1


def test_text_search_pagination_passes_token_in_body(gateway, respx_mock):
    page1 = _ts_response([_mk_place("ChIJ_p1_1")], next_page_token="tok-abc")
    page2 = _ts_response([_mk_place("ChIJ_p2_1")])

    route = respx_mock.post(_TEXT_SEARCH_URL)
    route.side_effect = [
        httpx.Response(200, json=page1),
        httpx.Response(200, json=page2),
    ]

    gateway.text_search("dentist", max_pages=2)

    assert route.call_count == 2
    first_body = json.loads(route.calls[0].request.content)
    second_body = json.loads(route.calls[1].request.content)
    assert "pageToken" not in first_body
    assert second_body["pageToken"] == "tok-abc"


# ── text_search: headers ─────────────────────────────────────────────────


def test_text_search_sends_field_mask_header(gateway, respx_mock):
    route = respx_mock.post(_TEXT_SEARCH_URL).respond(200, json={"places": []})

    gateway.text_search("dentist")

    mask = route.calls.last.request.headers.get("X-Goog-FieldMask", "")
    assert "places.id" in mask
    assert "places.displayName" in mask
    assert "places.formattedAddress" in mask
    assert "nextPageToken" in mask


def test_text_search_sends_api_key_header(gateway, respx_mock):
    route = respx_mock.post(_TEXT_SEARCH_URL).respond(200, json={"places": []})

    gateway.text_search("dentist")

    assert route.calls.last.request.headers["X-Goog-Api-Key"] == "test-api-key"


# ── text_search: retry behavior ──────────────────────────────────────────


def test_text_search_retries_on_500_then_succeeds(gateway, respx_mock):
    route = respx_mock.post(_TEXT_SEARCH_URL)
    route.side_effect = [
        httpx.Response(500, json={"error": "server error"}),
        httpx.Response(200, json={"places": [_mk_place("ChIJ_1")]}),
    ]

    results = gateway.text_search("dentist")

    assert len(results) == 1
    assert route.call_count == 2


def test_text_search_retries_on_429_then_succeeds(gateway, respx_mock):
    route = respx_mock.post(_TEXT_SEARCH_URL)
    route.side_effect = [
        httpx.Response(429, json={"error": "rate limited"}),
        httpx.Response(200, json={"places": [_mk_place("ChIJ_1")]}),
    ]

    results = gateway.text_search("dentist")

    assert len(results) == 1
    assert route.call_count == 2


def test_text_search_does_not_retry_on_400(gateway, respx_mock):
    route = respx_mock.post(_TEXT_SEARCH_URL).respond(
        400, json={"error": "bad request"}
    )

    with pytest.raises(GooglePlacesError, match="400"):
        gateway.text_search("dentist")

    assert route.call_count == 1


def test_text_search_does_not_retry_on_403(gateway, respx_mock):
    route = respx_mock.post(_TEXT_SEARCH_URL).respond(403, text="forbidden")

    with pytest.raises(GooglePlacesError, match="403"):
        gateway.text_search("dentist")

    assert route.call_count == 1


def test_text_search_raises_after_persistent_5xx(gateway, respx_mock):
    route = respx_mock.post(_TEXT_SEARCH_URL).respond(503, text="unavailable")

    with pytest.raises(httpx.HTTPStatusError):
        gateway.text_search("dentist")

    assert route.call_count == 3  # max_attempts


# ── text_search: input validation ────────────────────────────────────────


def test_text_search_rejects_blank_query(gateway):
    with pytest.raises(ValueError, match="query"):
        gateway.text_search("")
    with pytest.raises(ValueError, match="query"):
        gateway.text_search("   ")


def test_text_search_rejects_max_pages_below_one(gateway):
    with pytest.raises(ValueError, match="max_pages"):
        gateway.text_search("dentist", max_pages=0)
    with pytest.raises(ValueError, match="max_pages"):
        gateway.text_search("dentist", max_pages=-1)


# ── get_place_details ────────────────────────────────────────────────────


def test_get_place_details_returns_raw_dict(gateway, respx_mock, fixtures_dir):
    raw = json.loads((fixtures_dir / "place_details_sample.json").read_text())
    route = respx_mock.get(_details_url(raw["id"])).respond(200, json=raw)

    result = gateway.get_place_details(raw["id"])

    assert result == raw  # raw round-trip — no model lossy-ness
    assert route.call_count == 1


def test_get_place_details_field_mask_covers_critical_fields(gateway, respx_mock):
    route = respx_mock.get(_details_url("ChIJ_X")).respond(
        200, json={"id": "ChIJ_X", "displayName": {"text": "X"}}
    )

    gateway.get_place_details("ChIJ_X")

    mask = route.calls.last.request.headers.get("X-Goog-FieldMask", "")
    # If any of these drop out of the mask, lead quality breaks silently.
    for field in (
        "id",
        "displayName",
        "formattedAddress",
        "rating",
        "userRatingCount",
        "reviews",
        "editorialSummary",
        "websiteUri",
        "businessStatus",
    ):
        assert field in mask, f"required field {field!r} missing from field mask"


def test_get_place_details_sends_api_key_header(gateway, respx_mock):
    route = respx_mock.get(_details_url("ChIJ_X")).respond(
        200, json={"id": "ChIJ_X", "displayName": {"text": "X"}}
    )

    gateway.get_place_details("ChIJ_X")

    assert route.calls.last.request.headers["X-Goog-Api-Key"] == "test-api-key"


def test_get_place_details_retries_on_5xx(gateway, respx_mock):
    route = respx_mock.get(_details_url("ChIJ_X"))
    route.side_effect = [
        httpx.Response(502, text="bad gateway"),
        httpx.Response(200, json={"id": "ChIJ_X", "displayName": {"text": "X"}}),
    ]

    result = gateway.get_place_details("ChIJ_X")

    assert result["id"] == "ChIJ_X"
    assert route.call_count == 2


def test_get_place_details_raises_on_404(gateway, respx_mock):
    route = respx_mock.get(_details_url("ChIJ_BOGUS")).respond(404, text="not found")

    with pytest.raises(GooglePlacesError, match="404"):
        gateway.get_place_details("ChIJ_BOGUS")

    assert route.call_count == 1


def test_get_place_details_rejects_blank_place_id(gateway):
    with pytest.raises(ValueError, match="place_id"):
        gateway.get_place_details("")
    with pytest.raises(ValueError, match="place_id"):
        gateway.get_place_details("   ")
