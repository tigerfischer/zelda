"""`LybrateListing` — one entry from Lybrate's per-city dentist
directory.

Lybrate's catalog is doctor-keyed (not clinic-keyed like Practo's
clinics path). Each entry corresponds to a doctor + their primary
clinic; the doctor profile URL is the natural identity. A single
clinic with multiple dentists shows up as N entries.

Why a separate model from `PractoListing`?
------------------------------------------
Same rationale as keeping Google Places separate from Practo — each
source has its own shape and identity. Lybrate ships clean
schema.org `Physician` JSON-LD with fields Practo doesn't expose
(real phone numbers, qualifications, opening hours). Cross-source
matching merges Lybrate doctor entries into the clinic-level
canonical entity later.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LybrateListing(BaseModel):
    """One Lybrate dentist directory entry."""

    model_config = ConfigDict(extra="ignore")

    profile_url: str = Field(
        description="Absolute URL of the doctor profile page on lybrate.com",
    )
    city: str = Field(description="City name as supplied to discovery")
    doctor_name: str = Field(
        description="Doctor's name as shown on Lybrate",
    )

    clinic_name: str | None = Field(
        default=None,
        description="Clinic name (Lybrate doesn't always expose this)",
    )
    address: str | None = Field(
        default=None, description="Street address from JSON-LD",
    )
    locality: str | None = Field(
        default=None,
        description="Address locality / neighborhood (`addressLocality`)",
    )
    postal_code: str | None = Field(default=None)
    lat: float | None = Field(
        default=None, description="Latitude (decimal degrees)",
    )
    lng: float | None = Field(
        default=None, description="Longitude (decimal degrees)",
    )
    phone: str | None = Field(
        default=None,
        description=(
            "Phone number from JSON-LD or page DOM. Unlike Practo, "
            "Lybrate exposes the clinic's real number rather than a "
            "call-tracking proxy."
        ),
    )
    specialty: str | None = Field(
        default=None,
        description="`medicalSpecialty.name` — typically 'Dentist'",
    )

    raw_json: dict[str, Any] = Field(default_factory=dict)

    # Discovery housekeeping.
    discovered_at: datetime
    last_modified_at: datetime
    last_synced_at: datetime | None = None
