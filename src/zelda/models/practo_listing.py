"""`PractoListing` — one entry from Practo's per-city dental-clinics
directory.

This is a *discovery-phase* record. The discovery pipeline crawls
`practo.com/<city>/clinics/dental-clinics` once per city and persists
one `PractoListing` per clinic. Cross-source matching (linking a
Practo listing to a Google Places lead, an IDA member, etc.) is a
separate phase that reads from this table.

Identity
--------
The natural key is `profile_url` — Practo's stable per-clinic URL.
We persist the absolute URL (e.g. `https://www.practo.com/ludhiana/
clinic/jolly-dental-care-model-town-1`) rather than just the slug,
so the table is self-describing without joining on city.

Why a separate table from `google_places_leads`?
------------------------------------------------
Practo and Google Places have very different shapes:
- Google Places gives us rich Place Details (50+ fields, opening
  hours, photos, primary types, full address components).
- Practo's directory is intentionally minimal — name, address,
  geo coordinates, profile URL.

Cramming both into one "leads" table forces uncomfortable nullability
on every column. Per-source tables keep each source's native shape
intact; the future `clinics` table is the merged canonical entity.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PractoListing(BaseModel):
    """One Practo dental-clinic directory entry."""

    model_config = ConfigDict(extra="ignore")

    profile_url: str = Field(
        description="Absolute URL of the clinic profile page on practo.com",
    )
    city: str = Field(description="City name as supplied to discovery")
    name: str = Field(description="Clinic name as shown on Practo")

    address: str | None = Field(
        default=None, description="Street address from JSON-LD",
    )
    lat: float | None = Field(
        default=None, description="Latitude (decimal degrees)",
    )
    lng: float | None = Field(
        default=None, description="Longitude (decimal degrees)",
    )

    # Full extracted JSON for forward compatibility — anything we
    # don't promote to a column today is recoverable later.
    raw_json: dict[str, Any] = Field(default_factory=dict)

    # Discovery housekeeping — same convention as GooglePlacesLead.
    discovered_at: datetime
    last_modified_at: datetime
    last_synced_at: datetime | None = Field(
        default=None,
        description="Last time this row was mirrored to the Drive sheet",
    )
