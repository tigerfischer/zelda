"""`Lead` — the final output of the matching pipeline.

A lead is either:
- **enriched**: confirmed match across ≥2 sources; fields merged from all
  sources (richer data, higher confidence the clinic is real).
- **standalone**: appeared in exactly one source; no match found. Nothing
  is discarded — every row becomes a lead.

Source attribution (`google_places_id`, `practo_url`, `lybrate_urls`) lets
downstream phases re-join against per-source tables for additional data.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class Lead(BaseModel):
    lead_id: str                         # UUID
    city: str
    run_id: str
    tier: Literal["enriched", "standalone"]

    # Best-available fields (merged from all matched sources)
    name: str
    address: str | None = None
    lat: float | None = None
    lng: float | None = None
    phone: str | None = None
    website: str | None = None
    google_maps_url: str | None = None
    rating: float | None = None
    review_count: int | None = None

    # Source attribution
    google_places_id: str | None = None
    practo_url: str | None = None
    lybrate_urls: list[str] = Field(default_factory=list)

    # Match metadata
    match_confidence: float | None = None
    match_notes: str | None = None
    human_review_needed: bool = False

    # Raw source data for forward compatibility
    source_data: dict[str, Any] = Field(default_factory=dict)

    created_at: datetime


__all__ = ["Lead"]
