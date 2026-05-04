"""WhatsApp message personalizer — Phase 16b.

Takes the generic outreach template and a lead's enrichment signals, then
calls Claude to produce a clinic-specific first WhatsApp message.

The agent receives:
  - The generic template (anchor / reference)
  - A structured signal summary derived from LeadEnrichment
  - Strict guardrails: max 5 lines, no price, soft CTA, no emojis

Returns the personalized message as a plain string.

Usage:
    client = anthropic.Anthropic()
    personalizer = WhatsAppPersonalizer(client)

    ctx = lead_context_from_enrichment(enrichment)
    message = personalizer.personalize(ctx)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import anthropic
from loguru import logger

from zelda.models.lead_enrichment import LeadEnrichment


_GENERIC_TEMPLATE = """\
Hi Doctor — your clinic is likely leaving three things on the table every week:

• Google-generated new patients — the "dentist near me" searches that go to competitors with a better-optimized profile and more reviews
• Appointments lost to no-shows — because no WhatsApp reminder went out the day before
• Repeat patient visits — the 6-month recall that no one ever followed up on

Zelva handles all three automatically: we optimize your Google Business Profile, collect reviews, send appointment reminders, and bring lapsed patients back — all over WhatsApp, built specifically for Indian dental clinics.

Worth 5 minutes to see what this looks like for your clinic?"""


_SYSTEM_PROMPT = """\
You are a WhatsApp outreach copywriter for Zelva — a marketing platform built specifically for Indian dental clinics.

Your task: write a personalized first WhatsApp message for a specific dental clinic, using the signals provided.

REQUIRED STRUCTURE — every message must follow this exact three-part shape:

PART 1 — HOOK (1-2 sentences):
A specific observation about THIS clinic's biggest gap or missed opportunity. Lead with the outcome they are leaving on the table based on their signals.
- Few reviews (< 100): hook on the review gap and the Google searches they are losing
- Strong reviews (100+): briefly acknowledge it, then pivot to the next gap (no booking, missing GBP description, no recall system)
- Premium services (implants, orthodontics, cosmetic): mention that high-value patients are a missed review opportunity
- Open with "Hi Dr. [name]" if owner name is known, otherwise "Hi Doctor"

PART 2 — CAPABILITY BLOCK (always include all three bullets, always in this order):
Introduce with "Zelva helps with three things every dental clinic needs:" then:
• GBP profile optimization for more walk-ins and calls — tailor the wording: if few reviews, "we collect reviews automatically and optimize your profile so more patients find you on Google"; if already strong on reviews, "we optimize your profile so more Google searches convert into calls"
• Appointment reminders over WhatsApp — "so fewer patients no-show and every booked slot is filled"
• Patient recall — "automated messages for patients who haven't visited in 6 months, so they come back without you lifting a finger"

PART 3 — CTA (1 line):
Soft close: "Worth 5 minutes to see what this looks like for your clinic?" or similar. Never pushy.

Rules:
- Mention Zelva exactly once (to open Part 2)
- Maximum 7 lines total — this is a WhatsApp message, not an email
- No price mention ever
- No emojis
- No salesy superlatives ("revolutionary", "game-changing", "the best")
- Never invent statistics or percentages — say "significantly" or "meaningfully" instead of made-up numbers
- Output ONLY the message text — no preamble, no explanation, no surrounding quotes"""


@dataclass
class LeadContext:
    """The signals the personalizer uses. Build via lead_context_from_enrichment()."""

    clinic_name: str
    city: str
    owner_name: str | None = None
    google_review_count: int | None = None
    google_rating: float | None = None
    gbp_has_description: bool | None = None
    gbp_photos_count: int | None = None
    has_website: bool | None = None
    website_loads: bool | None = None
    has_online_booking: bool | None = None
    has_whatsapp_link: bool | None = None
    on_practo: bool | None = None
    on_lybrate: bool | None = None
    service_mix: list[str] = field(default_factory=list)
    review_velocity_30d: int | None = None
    pitch_angle: str | None = None


class WhatsAppPersonalizer:
    """Claude-powered agent that personalizes the generic WhatsApp template per lead."""

    def __init__(
        self,
        client: anthropic.Anthropic,
        model: str = "claude-haiku-4-5-20251001",
    ) -> None:
        self._client = client
        self._model = model

    def revise(self, ctx: LeadContext, current_message: str, instruction: str) -> str:
        """Re-run the agent with a reviewer instruction on top of the existing signals.

        The instruction is treated as an editorial note — e.g. "make it shorter",
        "focus on no-shows", "more conversational". The agent rewrites the message
        from scratch using both the lead signals and the instruction.
        """
        signals = _build_signal_summary(ctx)
        user_prompt = (
            f"Clinic signals:\n{signals}\n\n"
            f"Previously drafted message:\n{current_message}\n\n"
            f"Reviewer instruction:\n{instruction}\n\n"
            "Apply the instruction and rewrite the message. "
            "Keep signals that are still relevant. Follow all format rules."
        )
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=512,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return response.content[0].text.strip()
        except Exception as e:  # noqa: BLE001
            logger.error(
                "whatsapp_personalizer.revise_error clinic={n} err={e}",
                n=ctx.clinic_name, e=e,
            )
            return current_message  # fall back to the original if the API call fails

    def personalize(self, ctx: LeadContext) -> str:
        """Return a personalized WhatsApp first message for the clinic."""
        signals = _build_signal_summary(ctx)
        user_prompt = (
            f"Generic template:\n{_GENERIC_TEMPLATE}\n\n"
            f"Clinic signals:\n{signals}\n\n"
            "Write the personalized WhatsApp message."
        )
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=512,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return response.content[0].text.strip()
        except Exception as e:  # noqa: BLE001
            logger.error(
                "whatsapp_personalizer.error clinic={n} err={e}",
                n=ctx.clinic_name, e=e,
            )
            return _GENERIC_TEMPLATE.replace("[City]", ctx.city)


def lead_context_from_enrichment(enrichment: LeadEnrichment) -> LeadContext:
    """Build a LeadContext from a LeadEnrichment model instance."""
    return LeadContext(
        clinic_name=enrichment.clinic_name or "Unknown",
        city=enrichment.city,
        owner_name=enrichment.owner_name,
        google_review_count=enrichment.google_review_count,
        google_rating=enrichment.google_rating,
        gbp_has_description=enrichment.gbp_has_description,
        gbp_photos_count=enrichment.gbp_photos_count,
        has_website=enrichment.has_website,
        website_loads=enrichment.website_loads,
        has_online_booking=enrichment.has_online_booking,
        has_whatsapp_link=enrichment.has_whatsapp_link,
        on_practo=bool(enrichment.on_practo) if enrichment.on_practo is not None else None,
        on_lybrate=bool(enrichment.on_lybrate) if enrichment.on_lybrate is not None else None,
        service_mix=list(enrichment.service_mix) if enrichment.service_mix else [],
        review_velocity_30d=enrichment.review_velocity_30d,
        pitch_angle=enrichment.pitch_angle,
    )


# ── private ───────────────────────────────────────────────────────────

def _build_signal_summary(ctx: LeadContext) -> str:
    lines: list[str] = [
        f"Clinic name: {ctx.clinic_name}",
        f"City: {ctx.city}",
    ]

    if ctx.owner_name:
        lines.append(f"Owner/doctor name: {ctx.owner_name}")

    if ctx.google_review_count is not None:
        lines.append(f"Google reviews: {ctx.google_review_count}")
    if ctx.google_rating is not None:
        lines.append(f"Google rating: {ctx.google_rating} / 5.0")
    if ctx.review_velocity_30d is not None:
        lines.append(f"New reviews in last 30 days: {ctx.review_velocity_30d}")

    if ctx.gbp_has_description is not None:
        lines.append(f"GBP has description: {'yes' if ctx.gbp_has_description else 'no — missing'}")
    if ctx.gbp_photos_count is not None:
        lines.append(f"GBP photos: {ctx.gbp_photos_count}")

    # Website — distinguish "no URL" vs "URL exists but doesn't load" (Facebook page)
    if ctx.has_website and ctx.website_loads:
        lines.append("Website: yes, loads fine")
    elif ctx.has_website and not ctx.website_loads:
        lines.append("Website: URL on record but does not load (likely a Facebook page, not a real website)")
    elif ctx.has_website is False:
        lines.append("Website: none")

    if ctx.has_online_booking is not None:
        lines.append(f"Online booking: {'yes' if ctx.has_online_booking else 'no'}")
    if ctx.has_whatsapp_link is not None:
        lines.append(f"WhatsApp link on website: {'yes' if ctx.has_whatsapp_link else 'no'}")

    platforms: list[str] = []
    if ctx.on_practo:
        platforms.append("Practo")
    if ctx.on_lybrate:
        platforms.append("Lybrate")
    if platforms:
        lines.append(f"Listed on: {', '.join(platforms)}")

    if ctx.service_mix:
        lines.append(f"Services offered: {', '.join(ctx.service_mix)}")

    if ctx.pitch_angle:
        lines.append(f"Strongest pitch angle (from our scoring): {ctx.pitch_angle}")

    return "\n".join(lines)


__all__ = ["WhatsAppPersonalizer", "LeadContext", "lead_context_from_enrichment"]
