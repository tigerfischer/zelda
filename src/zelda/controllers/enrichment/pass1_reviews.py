"""Pass 1 — review history signals.

Reads all reviews from `ReviewRepository` (already scraped by
`FetchReviewsController`) and computes:

  - review_velocity_30d / 90d / 180d   — how many reviews in each window
  - owner_response_rate                 — fraction of reviews with an owner reply
  - owner_avg_response_days             — median days between review and reply
  - has_revenue_leak_signal             — keyword scan for "didn't pick up" etc.
  - negative_theme_flags                — LLM (Haiku) classification of low-rated reviews

If no reviews are stored for a lead, the velocity signals are set to 0
and other signals remain None — this itself is a meaningful signal (the
clinic has never been scraped or has zero reviews).

LLM call:
  One Haiku call per lead that has ≥1 low-rated review (≤3 stars).
  Batch-encodes all low-rated review texts into a single prompt.
  Cost: ~$0.001/lead.
"""

from __future__ import annotations

import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

import anthropic
from loguru import logger

from zelda.controllers.matching.prompt_loader import render_prompt
from zelda.models.lead import Lead
from zelda.models.lead_enrichment import LeadEnrichment
from zelda.models.review import Review
from zelda.repositories.review_repo import ReviewRepository

_PASS_NAME = "pass1"

# ── LLM tool schema ────────────────────────────────────────────────────

_REVIEW_THEMES_TOOL: dict[str, Any] = {
    "name": "review_themes",
    "description": "Record which operational problems appear in the reviews.",
    "input_schema": {
        "type": "object",
        "properties": {
            "no_reply": {
                "type": "boolean",
                "description": "Reviews mention unanswered calls, no response, couldn't reach.",
            },
            "wait_time": {
                "type": "boolean",
                "description": "Reviews mention long waits or poor scheduling.",
            },
            "billing": {
                "type": "boolean",
                "description": "Reviews mention billing disputes, overcharging, surprise fees.",
            },
            "hygiene": {
                "type": "boolean",
                "description": "Reviews mention cleanliness or sterilisation concerns.",
            },
            "pain": {
                "type": "boolean",
                "description": "Reviews mention unnecessary or excessive pain during treatment.",
            },
            "rude_staff": {
                "type": "boolean",
                "description": "Reviews mention rude, dismissive, or unprofessional staff.",
            },
        },
        "required": ["no_reply", "wait_time", "billing", "hygiene", "pain", "rude_staff"],
    },
}

# ── Revenue-leak keyword scan (fast, no LLM) ───────────────────────────

_REVENUE_LEAK_PATTERNS = re.compile(
    r"didn[\'']?t pick up"
    r"|no\s+reply"
    r"|not picking up"
    r"|phone not answered"
    r"|couldn[\'']?t reach"
    r"|unreachable"
    r"|not reachable"
    r"|no\s+response"
    r"|not responding"
    r"|never\s+(called|responds|responded|picks)"
    r"|phone\s+(switched\s+off|busy|unavailable)"
    r"|could not\s+(reach|contact|book)"
    r"|not\s+available\s+on\s+(call|phone)"
    r"|appointment\s+(cancelled|not confirmed)"
    r"|didn[\'']?t (show|turn) up"
    r"|no\s+show",
    re.IGNORECASE,
)


def run(
    lead: Lead,
    enrichment: LeadEnrichment,
    *,
    review_repo: ReviewRepository,
    anthropic_client: anthropic.Anthropic | None = None,
    llm_model: str = "claude-haiku-4-5-20251001",
) -> LeadEnrichment:
    """Compute Pass 1 signals from review history."""
    now = datetime.now(timezone.utc)

    if not lead.google_places_id:
        # No GP data → no reviews possible
        enrichment.review_velocity_30d = 0
        enrichment.review_velocity_90d = 0
        enrichment.review_velocity_180d = 0
        enrichment.passes_completed[_PASS_NAME] = now.isoformat()
        enrichment.updated_at = now
        return enrichment

    reviews = review_repo.get_reviews_for_place(lead.google_places_id)

    # ── Velocity ────────────────────────────────────────────────────────
    enrichment.review_velocity_30d = _count_reviews_since(reviews, now, days=30)
    enrichment.review_velocity_90d = _count_reviews_since(reviews, now, days=90)
    enrichment.review_velocity_180d = _count_reviews_since(reviews, now, days=180)

    # ── Owner response ──────────────────────────────────────────────────
    with_response = [r for r in reviews if r.owner_response_text]
    if reviews:
        enrichment.owner_response_rate = len(with_response) / len(reviews)
        lags = _response_lags_days(reviews)
        if lags:
            enrichment.owner_avg_response_days = round(statistics.median(lags), 1)

    # ── Revenue leak keyword scan ───────────────────────────────────────
    all_text = " ".join(r.text or "" for r in reviews)
    enrichment.has_revenue_leak_signal = bool(_REVENUE_LEAK_PATTERNS.search(all_text))

    # ── Negative theme classification (LLM) ────────────────────────────
    low_rated = [r for r in reviews if r.rating is not None and r.rating <= 3 and r.text]
    if low_rated and anthropic_client is not None:
        themes = _classify_themes(
            low_rated, lead.name, lead.city, anthropic_client, llm_model
        )
        enrichment.negative_theme_flags = themes
    elif not low_rated:
        enrichment.negative_theme_flags = []

    enrichment.passes_completed[_PASS_NAME] = now.isoformat()
    enrichment.updated_at = now

    logger.debug(
        "enrichment.pass1 lead_id={lid} reviews={n} "
        "velocity_30d={v30} response_rate={rr:.0%} "
        "revenue_leak={rl} themes={th}",
        lid=lead.lead_id,
        n=len(reviews),
        v30=enrichment.review_velocity_30d,
        rr=enrichment.owner_response_rate or 0.0,
        rl=enrichment.has_revenue_leak_signal,
        th=enrichment.negative_theme_flags,
    )
    return enrichment


# ── helpers ────────────────────────────────────────────────────────────

def _count_reviews_since(reviews: list[Review], now: datetime, *, days: int) -> int:
    cutoff = now - timedelta(days=days)
    return sum(
        1 for r in reviews
        if r.approx_publish_at is not None
        and r.approx_publish_at.replace(tzinfo=timezone.utc) >= cutoff
    )


def _response_lags_days(reviews: list[Review]) -> list[float]:
    """Return list of owner-response lag in days for each responded review."""
    lags = []
    for r in reviews:
        if (
            r.approx_publish_at is not None
            and r.owner_response_approx_at is not None
        ):
            lag = (
                r.owner_response_approx_at - r.approx_publish_at.replace(tzinfo=timezone.utc)
            ).total_seconds() / 86400
            if lag >= 0:
                lags.append(lag)
    return lags


def _classify_themes(
    reviews: list[Review],
    clinic_name: str,
    city: str,
    client: anthropic.Anthropic,
    model: str,
) -> list[str]:
    try:
        prompt = render_prompt(
            "enrichment/review_themes.j2",
            clinic_name=clinic_name,
            city=city,
            reviews=reviews,
        )
        response = client.messages.create(
            model=model,
            max_tokens=256,
            tools=[_REVIEW_THEMES_TOOL],
            tool_choice={"type": "tool", "name": "review_themes"},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in response.content:
            if block.type == "tool_use":
                flags: dict[str, bool] = block.input  # type: ignore[assignment]
                return [theme for theme, present in flags.items() if present]
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "enrichment.pass1.llm_error lead={n} err={e}", n=clinic_name, e=e
        )
    return []


__all__ = ["run"]
