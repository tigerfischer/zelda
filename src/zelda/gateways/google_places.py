"""Google Places API (New) gateway.

Wraps:
- POST /v1/places:searchText  (Text Search)
- GET  /v1/places/{place_id}  (Place Details)

Knows about HTTP, auth headers, field masks, pagination, and retries.
Returns parsed `Place` models for Text Search and raw dicts for Place
Details (so callers can preserve the full response without lossy
re-serialization through our model).
"""

import time
from typing import Any

import httpx
from loguru import logger
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

from zelda.models.place import Place


_BASE_URL = "https://places.googleapis.com/v1"

# Field masks control which fields the API returns AND how it bills us.
# Text Search only needs the place_id and the next-page-token; details
# come from a separate call.
_TEXT_SEARCH_FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,nextPageToken"
)

# Place Details mask covers everything we care about. Adding a field
# here is how you make Zelda see new information.
_PLACE_DETAILS_FIELDS = (
    "id",
    "displayName",
    "formattedAddress",
    "shortFormattedAddress",
    "addressComponents",
    "location",
    "nationalPhoneNumber",
    "internationalPhoneNumber",
    "websiteUri",
    "googleMapsUri",
    "rating",
    "userRatingCount",
    "businessStatus",
    "primaryType",
    "types",
    "priceLevel",
    "editorialSummary",
    "currentOpeningHours",
    "photos",
    "reviews",
)
_PLACE_DETAILS_FIELD_MASK = ",".join(_PLACE_DETAILS_FIELDS)


class GooglePlacesError(Exception):
    """Raised when the Places API responds with a permanent (4xx) error
    that won't be fixed by retrying."""


_RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.HTTPStatusError,
    httpx.TransportError,
)


class GooglePlacesGateway:
    """Thin wrapper over the Google Places API (New).

    Parameters
    ----------
    api_key:
        From GCP > APIs & Services > Credentials. Must be attached to a
        project with "Places API (New)" enabled and billing on.
    client:
        Optional injected httpx.Client. Tests pass a custom one;
        production lets the gateway create its own with a 30s timeout.
    page_delay_seconds:
        Delay between paginated Text Search requests. The new API
        requires the next-page-token to "ripen" briefly before it's
        accepted; ~2s is documented as safe.
    max_attempts:
        Total HTTP attempts per request including the first. 3 by default.
    backoff_seconds:
        Fixed wait between retries. Tests pass 0.
    """

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        page_delay_seconds: float = 2.0,
        max_attempts: int = 3,
        backoff_seconds: float = 0.5,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("api_key must be non-empty")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")

        self._api_key = api_key.strip()
        self._client = client or httpx.Client(timeout=30.0)
        self._page_delay_seconds = page_delay_seconds
        self._retrying = Retrying(
            stop=stop_after_attempt(max_attempts),
            wait=wait_fixed(backoff_seconds),
            retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
            reraise=True,
        )

    # ── lifecycle ────────────────────────────────────────────────────────

    def __enter__(self) -> "GooglePlacesGateway":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ── public API ───────────────────────────────────────────────────────

    def text_search(self, query: str, *, max_pages: int = 1) -> list[Place]:
        """Run a Text Search and return parsed Place objects.

        Each page returns up to 20 places. Pagination stops early if the
        API returns no `nextPageToken`. Between page requests we sleep
        `page_delay_seconds` because the new API needs the token to
        ripen.
        """
        if not query or not query.strip():
            raise ValueError("query must be non-empty")
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")

        url = f"{_BASE_URL}/places:searchText"
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self._api_key,
            "X-Goog-FieldMask": _TEXT_SEARCH_FIELD_MASK,
        }

        all_places: list[Place] = []
        page_token: str | None = None

        for page_num in range(1, max_pages + 1):
            body: dict[str, Any] = {"textQuery": query}
            if page_token:
                body["pageToken"] = page_token

            response = self._request("POST", url, headers=headers, json=body)
            payload = response.json()

            page_places = [Place.model_validate(p) for p in payload.get("places", [])]
            all_places.extend(page_places)

            logger.info(
                "places.text_search page={page} query={query!r} "
                "page_results={page_results} cumulative={cumulative} "
                "latency_ms={latency_ms:.0f}",
                page=page_num,
                query=query,
                page_results=len(page_places),
                cumulative=len(all_places),
                latency_ms=response.elapsed.total_seconds() * 1000,
            )

            page_token = payload.get("nextPageToken")
            if not page_token:
                break
            if page_num < max_pages:
                time.sleep(self._page_delay_seconds)

        return all_places

    def get_place_details(self, place_id: str) -> dict[str, Any]:
        """Fetch the full Place Details payload as a raw dict.

        Returning the raw dict (not a parsed model) means callers can
        store it verbatim. The converter in `models.place` parses it
        into a typed `PlaceDetails` for field access.
        """
        if not place_id or not place_id.strip():
            raise ValueError("place_id must be non-empty")

        url = f"{_BASE_URL}/places/{place_id}"
        headers = {
            "X-Goog-Api-Key": self._api_key,
            "X-Goog-FieldMask": _PLACE_DETAILS_FIELD_MASK,
        }
        response = self._request("GET", url, headers=headers)
        payload: dict[str, Any] = response.json()

        logger.info(
            "places.get_place_details place_id={place_id} latency_ms={latency_ms:.0f} "
            "has_reviews={has_reviews} has_website={has_website}",
            place_id=place_id,
            latency_ms=response.elapsed.total_seconds() * 1000,
            has_reviews=bool(payload.get("reviews")),
            has_website=bool(payload.get("websiteUri")),
        )
        return payload

    # ── internals ────────────────────────────────────────────────────────

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """HTTP request with retry. 5xx and 429 retry; other 4xx raise immediately."""

        def _do() -> httpx.Response:
            response = self._client.request(method, url, **kwargs)
            if response.status_code >= 500 or response.status_code == 429:
                # raises HTTPStatusError → caught by tenacity → retried
                response.raise_for_status()
            if not response.is_success:
                # 4xx other than 429: programmer error or auth issue.
                # Not retryable; surface immediately.
                raise GooglePlacesError(
                    f"Places API {response.status_code} {response.reason_phrase}: "
                    f"{response.text[:500]}"
                )
            return response

        return self._retrying(_do)
