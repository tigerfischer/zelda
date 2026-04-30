from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from zelda.models.google_places_lead import GooglePlacesLead


class _CamelBase(BaseModel):
    """Shared config: API responses use camelCase; we use snake_case in Python."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
    )


class DisplayName(_CamelBase):
    text: str
    language_code: str | None = None


class Location(_CamelBase):
    latitude: float
    longitude: float


class AddressComponent(_CamelBase):
    long_text: str | None = None
    short_text: str | None = None
    types: list[str] = []


class EditorialSummary(_CamelBase):
    text: str
    language_code: str | None = None


class Photo(_CamelBase):
    name: str | None = None
    width_px: int | None = None
    height_px: int | None = None


class Review(_CamelBase):
    name: str | None = None
    rating: int | None = None
    text: dict[str, Any] | None = None
    author_attribution: dict[str, Any] | None = None
    publish_time: str | None = None


class OpeningHours(_CamelBase):
    open_now: bool | None = None
    periods: list[dict[str, Any]] | None = None
    weekday_descriptions: list[str] | None = None


class Place(_CamelBase):
    """Result from Text Search — minimal fields."""

    id: str
    display_name: DisplayName
    formatted_address: str | None = None


class PlaceDetails(_CamelBase):
    """Result from Place Details — full payload."""

    id: str
    display_name: DisplayName
    formatted_address: str | None = None
    short_formatted_address: str | None = None
    address_components: list[AddressComponent] | None = None
    location: Location | None = None
    national_phone_number: str | None = None
    international_phone_number: str | None = None
    website_uri: str | None = None
    google_maps_uri: str | None = None
    rating: float | None = None
    user_rating_count: int | None = None
    business_status: str | None = None
    primary_type: str | None = None
    types: list[str] | None = None
    price_level: str | None = None
    editorial_summary: EditorialSummary | None = None
    current_opening_hours: OpeningHours | None = None
    photos: list[Photo] | None = None
    reviews: list[Review] | None = None


def google_places_lead_from_place_details(
    raw: dict[str, Any],
    city: str,
    *,
    now: datetime | None = None,
) -> GooglePlacesLead:
    """Parse a raw Place Details API response and build a GooglePlacesLead.

    The full `raw` dict is preserved on the lead's `raw_json` field so we
    never drop information that our model didn't anticipate.
    """
    parsed = PlaceDetails.model_validate(raw)
    timestamp = now or datetime.now(timezone.utc)

    return GooglePlacesLead(
        place_id=parsed.id,
        city=city,
        name=parsed.display_name.text,
        formatted_address=parsed.formatted_address,
        short_address=parsed.short_formatted_address,
        address_components=(
            [c.model_dump(by_alias=True, exclude_none=True) for c in parsed.address_components]
            if parsed.address_components
            else None
        ),
        lat=parsed.location.latitude if parsed.location else None,
        lng=parsed.location.longitude if parsed.location else None,
        phone=parsed.national_phone_number,
        phone_intl=parsed.international_phone_number,
        website=parsed.website_uri,
        google_maps_url=parsed.google_maps_uri,
        rating=parsed.rating,
        review_count=parsed.user_rating_count,
        reviews=(
            [r.model_dump(by_alias=True, exclude_none=True) for r in parsed.reviews]
            if parsed.reviews
            else None
        ),
        business_status=parsed.business_status,
        primary_type=parsed.primary_type,
        types=parsed.types,
        price_level=parsed.price_level,
        editorial_summary=parsed.editorial_summary.text if parsed.editorial_summary else None,
        photos_count=len(parsed.photos) if parsed.photos else None,
        opening_hours=(
            parsed.current_opening_hours.model_dump(by_alias=True, exclude_none=True)
            if parsed.current_opening_hours
            else None
        ),
        raw_json=raw,
        discovered_at=timestamp,
        last_modified_at=timestamp,
    )
