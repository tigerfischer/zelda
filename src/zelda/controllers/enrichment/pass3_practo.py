"""Pass 3 — Practo signals.

Reads from two Practo data sources (whichever exists):

1. `practo_profiles` table — detailed profile data fetched by the
   existing `EnrichmentOrchestrator` (Phase 11). Contains consultation
   fee, rating, operating hours, raw_json.

2. `practo_listings` table — directory-level data from Phase 12-13.
   Minimal (name, address, lat/lng) — useful only for confirming
   presence, which Pass 0 already handles.

Signals produced:
  - practo_consultation_fee_inr  — from practo_profiles.consultation_fee
  - practo_review_count          — parsed from practo_profiles.raw_json
  - practo_rating                — from practo_profiles.rating
  - practo_booking_enabled       — inferred from practo_profiles.raw_json
  - years_in_operation           — from practo_profiles.raw_json "experience" field
  - dentist_count                — count of doctors on the practo listing
  - owner_name                   — from practo_profiles.raw_json (if not already set)
  - owner_qualifications         — from practo_profiles.raw_json

If `practo_profiles` is not available (table doesn't exist or no row
for this lead), all signals remain None. Pass 3 is entirely non-blocking.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone

from loguru import logger

from zelda.models.lead import Lead
from zelda.models.lead_enrichment import LeadEnrichment

_PASS_NAME = "pass3"

# ── Regex helpers for raw_json field extraction ────────────────────────

_EXPERIENCE_RE = re.compile(r"(\d+)\s*(?:year|yr)", re.IGNORECASE)
_FEE_RE = re.compile(r"(?:₹|Rs\.?|INR)\s*([\d,]+)", re.IGNORECASE)


def run(
    lead: Lead,
    enrichment: LeadEnrichment,
    *,
    db_conn: sqlite3.Connection,
) -> LeadEnrichment:
    """Compute Pass 3 signals from Practo data sources.

    `db_conn` is a raw sqlite3 connection — this pass reads from
    whichever Practo tables exist without requiring specific repo objects,
    so it degrades gracefully if the tables are absent.
    """
    now = datetime.now(timezone.utc)

    if not lead.practo_url:
        enrichment.passes_completed[_PASS_NAME] = now.isoformat()
        enrichment.updated_at = now
        return enrichment

    # ── Try practo_profiles first (richest data source) ────────────────
    profile_row = _get_practo_profile(db_conn, lead.google_places_id)
    if profile_row:
        _apply_profile_signals(enrichment, profile_row, lead)
    else:
        # ── Fallback: parse practo_listings.raw_json ───────────────────
        listing_row = _get_practo_listing(db_conn, lead.practo_url)
        if listing_row:
            _apply_listing_signals(enrichment, listing_row)

    enrichment.passes_completed[_PASS_NAME] = now.isoformat()
    enrichment.updated_at = now

    logger.debug(
        "enrichment.pass3 lead_id={lid} fee={fee} rating={r} booking={b}",
        lid=lead.lead_id,
        fee=enrichment.practo_consultation_fee_inr,
        r=enrichment.practo_rating,
        b=enrichment.practo_booking_enabled,
    )
    return enrichment


# ── practo_profiles helpers ────────────────────────────────────────────

def _get_practo_profile(
    conn: sqlite3.Connection, place_id: str | None
) -> sqlite3.Row | None:
    if not place_id:
        return None
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM practo_profiles WHERE place_id = ? AND fetch_status = 'ok'",
            (place_id,),
        ).fetchone()
        return row
    except sqlite3.OperationalError:
        return None  # table doesn't exist


def _apply_profile_signals(
    enrichment: LeadEnrichment,
    row: sqlite3.Row,
    lead: Lead,
) -> None:
    enrichment.practo_consultation_fee_inr = row["consultation_fee"]
    enrichment.practo_rating = row["rating"]

    raw: dict = {}
    try:
        raw = json.loads(row["raw_json"] or "{}")
    except Exception:  # noqa: BLE001
        pass

    # Review count from raw_json (Practo stores it as "patient_stories" or
    # "total_feedback")
    enrichment.practo_review_count = (
        raw.get("patient_stories_count")
        or raw.get("total_feedback")
        or raw.get("review_count")
    )

    # Booking availability
    enrichment.practo_booking_enabled = bool(
        raw.get("is_clinic_bookable")
        or raw.get("booking_enabled")
        or raw.get("has_appointment")
    )

    # Experience / years in operation
    exp_text = str(raw.get("experience") or raw.get("years_of_experience") or "")
    m = _EXPERIENCE_RE.search(exp_text)
    if m:
        enrichment.years_in_operation = int(m.group(1))

    # Doctor count
    doctors = raw.get("doctors") or raw.get("doctor_list") or []
    if isinstance(doctors, list) and doctors:
        enrichment.dentist_count = len(doctors)

    # Owner name / qualifications (don't clobber Lybrate data if already set)
    if not enrichment.owner_name:
        primary_doc = (doctors[0] if doctors else {})
        if isinstance(primary_doc, dict):
            enrichment.owner_name = primary_doc.get("name") or None
            enrichment.owner_qualifications = primary_doc.get("qualification") or None


# ── practo_listings fallback ───────────────────────────────────────────

def _get_practo_listing(
    conn: sqlite3.Connection, profile_url: str
) -> sqlite3.Row | None:
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM practo_listings WHERE profile_url = ?",
            (profile_url,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def _apply_listing_signals(
    enrichment: LeadEnrichment, row: sqlite3.Row
) -> None:
    raw: dict = {}
    try:
        raw = json.loads(row["raw_json"] or "{}")
    except Exception:  # noqa: BLE001
        pass

    # The directory listing raw_json only has name/address/lat/lng for now.
    # Future: if the directory scraper is extended to capture fee/rating, read here.
    if fee := raw.get("consultation_fee") or raw.get("fee"):
        fee_str = str(fee)
        m = _FEE_RE.search(fee_str)
        if m:
            enrichment.practo_consultation_fee_inr = int(m.group(1).replace(",", ""))


__all__ = ["run"]
