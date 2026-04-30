from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class GooglePlacesLead(BaseModel):
    """A raw lead as emitted by the discovery stage.

    Flat columns hold the fields we care about explicitly. `extras` holds
    fields we extract opportunistically but haven't promoted to columns.
    `raw_json` holds the full original Place Details API response so we
    never lose information.
    """

    model_config = ConfigDict(extra="ignore")

    place_id: str
    city: str
    name: str

    formatted_address: str | None = None
    short_address: str | None = None
    address_components: list[dict[str, Any]] | None = None
    lat: float | None = None
    lng: float | None = None

    phone: str | None = None
    phone_intl: str | None = None
    website: str | None = None
    google_maps_url: str | None = None

    rating: float | None = None
    review_count: int | None = None
    reviews: list[dict[str, Any]] | None = None

    business_status: str | None = None
    primary_type: str | None = None
    types: list[str] | None = None
    price_level: str | None = None
    editorial_summary: str | None = None
    photos_count: int | None = None
    opening_hours: dict[str, Any] | None = None

    extras: dict[str, Any] = Field(default_factory=dict)
    raw_json: dict[str, Any] = Field(default_factory=dict)

    discovered_at: datetime
    last_modified_at: datetime
    last_synced_at: datetime | None = None
