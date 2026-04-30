"""Unified projection of any per-source row for use by the matching pipeline.

Each source (Google Places, Practo, Lybrate) has its own model shape.
`MatchableRow` is the common interface the pre-filter and LLM judge work with,
so matching logic stays source-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass

from zelda.models.google_places_lead import GooglePlacesLead
from zelda.models.lybrate_listing import LybrateListing
from zelda.models.practo_listing import PractoListing


@dataclass(frozen=True)
class MatchableRow:
    source: str       # "google_places" | "practo" | "lybrate"
    key: str          # natural key: place_id or profile_url
    name: str
    city: str
    address: str | None = None
    lat: float | None = None
    lng: float | None = None
    phone: str | None = None
    website: str | None = None
    google_maps_url: str | None = None
    rating: float | None = None
    review_count: int | None = None


def from_google_places(lead: GooglePlacesLead) -> MatchableRow:
    return MatchableRow(
        source="google_places",
        key=lead.place_id,
        name=lead.name,
        city=lead.city,
        address=lead.formatted_address or lead.short_address,
        lat=lead.lat,
        lng=lead.lng,
        phone=lead.phone or lead.phone_intl,
        website=lead.website,
        google_maps_url=lead.google_maps_url,
        rating=lead.rating,
        review_count=lead.review_count,
    )


def from_practo(listing: PractoListing) -> MatchableRow:
    return MatchableRow(
        source="practo",
        key=listing.profile_url,
        name=listing.name,
        city=listing.city,
        address=listing.address,
        lat=listing.lat,
        lng=listing.lng,
    )


def from_lybrate(listing: LybrateListing) -> MatchableRow:
    return MatchableRow(
        source="lybrate",
        key=listing.profile_url,
        name=listing.doctor_name,
        city=listing.city,
        address=listing.address,
        lat=listing.lat,
        lng=listing.lng,
        phone=listing.phone,
    )


__all__ = [
    "MatchableRow",
    "from_google_places",
    "from_practo",
    "from_lybrate",
]
