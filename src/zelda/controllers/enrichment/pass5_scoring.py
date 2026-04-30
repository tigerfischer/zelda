"""Pass 5 — lead scoring.

Pure computation — no I/O, no LLM, no network. Reads the signals
written by Passes 0–3 and produces:

  need_score  (int 0–100)   — how urgently does this clinic need Zelda?
  score_tier  (str)          — "hot" | "warm" | "cold" | "disqualified"
  pitch_angle (str)          — the strongest pain point to lead with

Scoring components (each 0–100, then blended):

  REPUTATION   (weight 30%)
    - Low review count:   <20 → 100, 20–50 → 60, 50–100 → 30, >100 → 0
    - Stagnant velocity:  0 in 90d → +30, 1–2 in 90d → +15
    - No owner responses: rate = 0 → +20, rate < 0.3 → +10
    - Revenue leak found:            → +20 (capped at 100)

  ACQUISITION  (weight 25%)
    - No website:         → 80
    - Website doesn't load: → 60
    - Missing GBP hours:  → +20
    - Missing GBP photos: → +15
    - Missing description: → +10
    - Single source only:  → +20 (not on Practo or Lybrate)

  CONVERSION   (weight 25%)
    - No WhatsApp link:   → +30
    - No online booking:  → +25
    - No chat widget:     → +10

  SIZE/FIT     (weight 20%)
    - On Practo (reachable): → +20
    - Direct phone known:    → +20
    - Multi-dentist clinic:  → +10
    - Chain → disqualify

Tiers:
  70–100 → hot
  45–69  → warm
  20–44  → cold
  0–19   → cold (but still kept)
  Disqualified → set to 0, tier = "disqualified"

Pitch angle picks the highest sub-score domain as the opening line.
"""

from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger

from zelda.models.lead import Lead
from zelda.models.lead_enrichment import LeadEnrichment

_PASS_NAME = "pass5"


def run(lead: Lead, enrichment: LeadEnrichment) -> LeadEnrichment:
    """Compute the lead score. Returns updated enrichment."""
    now = datetime.now(timezone.utc)

    # ── Disqualifiers ───────────────────────────────────────────────────
    if _is_disqualified(enrichment):
        enrichment.need_score = 0
        enrichment.score_tier = "disqualified"
        enrichment.pitch_angle = "disqualified"
        enrichment.passes_completed[_PASS_NAME] = now.isoformat()
        enrichment.updated_at = now
        return enrichment

    # ── Component scores ────────────────────────────────────────────────
    rep_score = _reputation_score(enrichment)
    acq_score = _acquisition_score(enrichment)
    conv_score = _conversion_score(enrichment)
    fit_score = _fit_score(enrichment)

    # Weighted blend
    raw = (
        rep_score * 0.30
        + acq_score * 0.25
        + conv_score * 0.25
        + fit_score * 0.20
    )
    need_score = min(100, max(0, round(raw)))

    enrichment.need_score = need_score
    enrichment.score_tier = _tier(need_score)
    enrichment.pitch_angle = _pitch_angle(rep_score, acq_score, conv_score, fit_score)

    enrichment.passes_completed[_PASS_NAME] = now.isoformat()
    enrichment.updated_at = now

    logger.debug(
        "enrichment.pass5 lead_id={lid} score={s} tier={t} pitch={p} "
        "rep={r} acq={a} conv={c} fit={f}",
        lid=lead.lead_id,
        s=need_score,
        t=enrichment.score_tier,
        p=enrichment.pitch_angle,
        r=rep_score,
        a=acq_score,
        c=conv_score,
        f=fit_score,
    )
    return enrichment


# ── Disqualifiers ──────────────────────────────────────────────────────

def _is_disqualified(e: LeadEnrichment) -> bool:
    if e.is_chain:
        return True
    if e.is_hospital_embedded:
        return True
    if e.is_not_operational:
        return True
    return False


# ── Component scorers ──────────────────────────────────────────────────

def _reputation_score(e: LeadEnrichment) -> float:
    score = 0.0

    # Low review count (0–40 points)
    count = e.google_review_count
    if count is None:
        score += 30  # unknown → moderate need assumed
    elif count < 20:
        score += 40
    elif count < 50:
        score += 25
    elif count < 100:
        score += 10

    # Stagnant velocity (0–30 points)
    v90 = e.review_velocity_90d
    if v90 is not None:
        if v90 == 0:
            score += 30
        elif v90 <= 2:
            score += 15

    # No owner responses (0–20 points)
    rr = e.owner_response_rate
    if rr is None:
        pass  # no data
    elif rr == 0:
        score += 20
    elif rr < 0.3:
        score += 10

    # Revenue leak signal (0–20 bonus)
    if e.has_revenue_leak_signal:
        score += 20

    return min(100.0, score)


def _acquisition_score(e: LeadEnrichment) -> float:
    score = 0.0

    if e.has_website is False:
        score += 50
    elif e.website_loads is False:
        score += 40

    if not e.gbp_has_hours:
        score += 20
    if (e.gbp_photos_count or 0) < 5:
        score += 15
    if not e.gbp_has_description:
        score += 10

    # Only on one platform
    if (e.source_count or 1) == 1:
        score += 20
    elif not e.on_practo:
        score += 10

    return min(100.0, score)


def _conversion_score(e: LeadEnrichment) -> float:
    score = 0.0

    if e.has_whatsapp_link is False:
        score += 35
    if e.has_online_booking is False:
        score += 30
    if e.practo_booking_enabled is False:
        score += 15
    if e.has_chat_widget is False:
        score += 10

    return min(100.0, score)


def _fit_score(e: LeadEnrichment) -> float:
    score = 0.0

    if e.on_practo:
        score += 20  # reachable via known channel
    if e.direct_phone:
        score += 20  # can cold-call
    if (e.dentist_count or 1) > 1:
        score += 10  # larger practice → more willingness to spend
    if e.practo_consultation_fee_inr and e.practo_consultation_fee_inr >= 300:
        score += 15  # charges a reasonable fee → not a budget clinic
    if e.nap_consistent is False:
        score += 10  # NAP inconsistency → they need help with listings

    return min(100.0, score)


# ── Tier + pitch angle ─────────────────────────────────────────────────

def _tier(score: int) -> str:
    if score >= 70:
        return "hot"
    if score >= 45:
        return "warm"
    return "cold"


def _pitch_angle(
    rep: float, acq: float, conv: float, fit: float
) -> str:
    best = max(
        (rep, "reviews"),
        (acq, "gbp"),
        (conv, "booking"),
        (fit, "recall"),
    )
    return best[1]


__all__ = ["run"]
