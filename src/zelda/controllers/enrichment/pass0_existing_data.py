"""Pass 0 — compute signals from data already in the database.

No network calls, no LLM, no Playwright. Reads from:
  - google_places_leads   (GP data from discovery)
  - leads                 (unified lead with source attribution)
  - lybrate_listings      (doctor name, phone, specialty)
  - practo_listings       (confirms on_practo)

Signals produced:
  - google_review_count, google_rating
  - gbp_has_hours, gbp_photos_count, gbp_has_description
  - is_not_operational, has_website
  - on_practo, on_lybrate, source_count
  - nap_consistent
  - is_chain, is_hospital_embedded
  - owner_name, owner_qualifications, direct_phone
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from loguru import logger

from zelda.controllers.enrichment.chain_detection import detect_chain, detect_hospital
from zelda.models.lead import Lead
from zelda.models.lead_enrichment import LeadEnrichment
from zelda.repositories.google_places_lead_repo import GooglePlacesLeadRepository
from zelda.repositories.lybrate_listing_repo import LybrateListingRepository
from zelda.repositories.practo_listing_repo import PractoListingRepository

_PASS_NAME = "pass0"


def run(
    lead: Lead,
    enrichment: LeadEnrichment,
    *,
    gp_repo: GooglePlacesLeadRepository,
    practo_repo: PractoListingRepository,
    lybrate_repo: LybrateListingRepository,
) -> LeadEnrichment:
    """Compute Pass 0 signals. Returns updated enrichment."""
    now = datetime.now(timezone.utc)

    # ── Identity ────────────────────────────────────────────────────────
    enrichment.clinic_name = lead.name

    # ── Google Places data ──────────────────────────────────────────────
    gp = gp_repo.get_by_id(lead.google_places_id) if lead.google_places_id else None

    if gp:
        enrichment.google_review_count = gp.review_count
        enrichment.google_rating = gp.rating
        enrichment.gbp_has_hours = gp.opening_hours is not None
        enrichment.gbp_photos_count = gp.photos_count
        enrichment.gbp_has_description = bool(gp.editorial_summary)
        enrichment.is_not_operational = (
            gp.business_status not in (None, "OPERATIONAL")
        )
        enrichment.has_website = bool(gp.website)
    else:
        # No GP record — clear any stale GP signals so force-reruns are idempotent
        enrichment.google_review_count = None
        enrichment.google_rating = None
        enrichment.gbp_has_hours = None
        enrichment.gbp_photos_count = None
        enrichment.gbp_has_description = None
        enrichment.is_not_operational = None
        enrichment.has_website = bool(lead.website)

    # ── Source presence ─────────────────────────────────────────────────
    enrichment.on_practo = bool(lead.practo_url)
    enrichment.on_lybrate = len(lead.lybrate_urls) > 0

    sources_present = sum([
        bool(lead.google_places_id),
        bool(lead.practo_url),
        len(lead.lybrate_urls) > 0,
    ])
    enrichment.source_count = sources_present

    # ── NAP consistency ─────────────────────────────────────────────────
    enrichment.nap_consistent = _check_nap_consistency(
        lead, gp_repo, practo_repo, lybrate_repo
    )

    # ── Chain / hospital detection ──────────────────────────────────────
    enrichment.is_chain = detect_chain(lead.name)
    enrichment.is_hospital_embedded = detect_hospital(lead.name, lead.address)

    # ── Owner / outreach from Lybrate ───────────────────────────────────
    if lead.lybrate_urls:
        first_url = lead.lybrate_urls[0]
        ly = lybrate_repo.get_by_url(first_url)
        if ly:
            enrichment.owner_name = ly.doctor_name or None
            enrichment.owner_qualifications = ly.specialty or None
            enrichment.direct_phone = ly.phone or lead.phone

    # Fallback: use lead-level phone if no Lybrate phone
    if not enrichment.direct_phone:
        enrichment.direct_phone = lead.phone

    # ── Mark pass complete ──────────────────────────────────────────────
    enrichment.passes_completed[_PASS_NAME] = now.isoformat()
    enrichment.updated_at = now

    logger.debug(
        "enrichment.pass0 lead_id={lid} name={name} "
        "reviews={rc} rating={r} on_practo={p} on_lybrate={l} "
        "is_chain={c}",
        lid=lead.lead_id,
        name=lead.name,
        rc=enrichment.google_review_count,
        r=enrichment.google_rating,
        p=enrichment.on_practo,
        l=enrichment.on_lybrate,
        c=enrichment.is_chain,
    )
    return enrichment


# ── NAP consistency helper ─────────────────────────────────────────────

def _check_nap_consistency(
    lead: Lead,
    gp_repo: GooglePlacesLeadRepository,
    practo_repo: PractoListingRepository,
    lybrate_repo: LybrateListingRepository,
) -> bool | None:
    """Check if name + phone are consistent across available sources.

    Returns True if consistent, False if conflict detected, None if
    only one source (nothing to compare).
    """
    phones: list[str] = []
    names: list[str] = []

    gp = gp_repo.get_by_id(lead.google_places_id) if lead.google_places_id else None
    if gp:
        if gp.phone:
            phones.append(_normalise_phone(gp.phone))
        names.append(gp.name)

    if lead.practo_url:
        practo = practo_repo.get_by_url(lead.practo_url)
        if practo:
            names.append(practo.name)

    if lead.lybrate_urls:
        ly = lybrate_repo.get_by_url(lead.lybrate_urls[0])
        if ly:
            if ly.phone:
                phones.append(_normalise_phone(ly.phone))

    # Need at least two data points to assess consistency
    if len(phones) < 2 and len(names) < 2:
        return None

    phone_ok = len(set(phones)) <= 1 if len(phones) >= 2 else True
    name_ok = _names_similar(names) if len(names) >= 2 else True
    return phone_ok and name_ok


_PHONE_DIGITS_RE = re.compile(r"\D")

def _normalise_phone(phone: str) -> str:
    digits = _PHONE_DIGITS_RE.sub("", phone)
    # Keep last 10 digits for Indian numbers (drop +91 prefix)
    return digits[-10:] if len(digits) >= 10 else digits


_NAME_NOISE_RE = re.compile(
    r"\b(dental|clinic|dentist|care|centre|center|dr|and|&|the)\b",
    re.IGNORECASE,
)

def _normalise_name(name: str) -> frozenset[str]:
    cleaned = _NAME_NOISE_RE.sub(" ", name.lower())
    return frozenset(t for t in cleaned.split() if len(t) > 1)


def _names_similar(names: list[str]) -> bool:
    """True if all name token sets share at least one non-trivial token."""
    token_sets = [_normalise_name(n) for n in names]
    if not all(token_sets):
        return True  # can't compare empty sets
    common = token_sets[0]
    for ts in token_sets[1:]:
        common = common & ts
    return len(common) >= 1


__all__ = ["run"]
