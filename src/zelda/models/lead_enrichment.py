"""`LeadEnrichment` — all computed enrichment signals for one lead.

One row per lead, keyed by `lead_id`. Each pass writes its slice of
signals and records itself in `passes_completed`. Passes are idempotent
and independently resumable — if Pass 1 is interrupted halfway through
a city, re-running picks up from where it left off.

Signal naming convention follows the catalog in docs/enrichment-signals.md:
  - Reputation:     google_review_count, google_rating, review_velocity_*, ...
  - Acquisition:    has_website, website_loads, gbp_has_hours, on_practo, ...
  - Conversion:     has_whatsapp_link, has_online_booking, practo_booking_enabled, ...
  - Ability to Pay: practo_consultation_fee_inr, service_mix, dentist_count, ...
  - Owner/Outreach: owner_name, owner_qualifications, direct_phone, ...
  - Disqualifiers:  is_chain, is_hospital_embedded, is_not_operational
  - Score:          need_score (0-100), score_tier, pitch_angle
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class LeadEnrichment(BaseModel):

    # ── identity ────────────────────────────────────────────────────────
    lead_id: str
    city: str
    clinic_name: str | None = None   # copied from Lead.name by Pass 0

    # ── reputation signals ──────────────────────────────────────────────
    # From Google Places API (Pass 0)
    google_review_count: int | None = None
    google_rating: float | None = None
    gbp_has_hours: bool | None = None
    gbp_photos_count: int | None = None
    gbp_has_description: bool | None = None
    is_not_operational: bool | None = None

    # From full review history (Pass 1)
    review_velocity_30d: int | None = None
    review_velocity_90d: int | None = None
    review_velocity_180d: int | None = None
    owner_response_rate: float | None = None        # 0.0–1.0
    owner_avg_response_days: float | None = None    # median days to reply
    has_revenue_leak_signal: bool | None = None     # "didn't pick up" etc in reviews
    negative_theme_flags: list[str] = Field(default_factory=list)
    # possible themes: no_reply, wait_time, billing, hygiene, pain, rude_staff

    # From Practo (Pass 3)
    practo_review_count: int | None = None
    practo_rating: float | None = None

    # ── acquisition signals ─────────────────────────────────────────────
    # From existing DB (Pass 0)
    has_website: bool | None = None
    on_practo: bool | None = None
    on_lybrate: bool | None = None
    source_count: int | None = None                 # 1, 2, or 3
    nap_consistent: bool | None = None              # name/phone consistent across sources
    is_chain: bool | None = None
    is_hospital_embedded: bool | None = None

    # From website audit (Pass 2)
    website_loads: bool | None = None
    website_is_mobile_friendly: bool | None = None
    website_has_schema_markup: bool | None = None
    website_has_blog: bool | None = None
    website_agency_credit: str | None = None        # agency name if found

    # ── conversion signals ──────────────────────────────────────────────
    # From website audit (Pass 2)
    has_whatsapp_link: bool | None = None
    has_online_booking: bool | None = None
    has_chat_widget: bool | None = None

    # From Practo (Pass 3)
    practo_booking_enabled: bool | None = None

    # ── ability-to-pay signals ──────────────────────────────────────────
    # From Practo (Pass 3)
    practo_consultation_fee_inr: int | None = None

    # From website (Pass 2) + LLM classification
    service_mix: list[str] = Field(default_factory=list)
    # e.g. ["general", "implants", "orthodontics", "cosmetic", "paediatric"]

    equipment_claims: list[str] = Field(default_factory=list)
    # e.g. ["cbct", "opg", "laser", "intraoral_scanner"]

    # From Practo / Lybrate (Pass 3)
    years_in_operation: int | None = None
    dentist_count: int | None = None

    # ── owner / outreach signals ────────────────────────────────────────
    # From Lybrate / Practo (Pass 0 + Pass 3)
    owner_name: str | None = None
    owner_qualifications: str | None = None         # "BDS", "MDS (Orthodontics)"
    direct_phone: str | None = None                 # Lybrate real number preferred

    # ── composite score ─────────────────────────────────────────────────
    # Computed in Pass 5 from all signals above
    need_score: int | None = None                   # 0–100
    score_tier: str | None = None                   # "hot" | "warm" | "cold" | "disqualified"
    pitch_angle: str | None = None
    # "reviews" | "gbp" | "booking" | "recall" | "disqualified"

    # ── metadata ────────────────────────────────────────────────────────
    passes_completed: dict[str, str] = Field(default_factory=dict)
    # { "pass0": "2026-04-30T18:00:00Z", "pass1": ..., ... }

    signal_extras: dict[str, Any] = Field(default_factory=dict)
    # Catch-all for opportunistic / experimental signals

    enrichment_version: str = "1"
    updated_at: datetime | None = None


__all__ = ["LeadEnrichment"]
