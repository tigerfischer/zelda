"""Call brief agent — Phase 16e.

Generates a personalized pre-call brief for Vaibhav to read on Telegram
before calling a lead at T+2 days after the WhatsApp message was sent.

The brief is compact and actionable:
  - Clinic snapshot (key signals in 3 lines)
  - What the WhatsApp conversation revealed about them so far
  - Suggested opening line tailored to their profile
  - 2-3 targeted discovery questions (not generic — based on their signals)
  - Which Zelva capability to lead with and why
  - Most likely objection to expect, and how to handle it
  - If a call transcript exists from a previous call, key context from it

The generic call_brief_generic_v1.md is the playbook — this agent
personalizes it per lead rather than replacing it.
"""

from __future__ import annotations

from pathlib import Path

import anthropic
from loguru import logger

from zelda.models.outreach_message import ConversationTurn
from zelda.outreach.whatsapp_personalizer import LeadContext

_CALL_BRIEF_DOC = (
    Path(__file__).parent.parent.parent.parent / "docs" / "outreach" / "call_brief_generic_v1.md"
)

_SYSTEM_PROMPT = """\
You are a sales coach for Zelva — a WhatsApp-based marketing platform built specifically for Indian dental clinics.

Zelva's three core capabilities:
1. Google Business Profile optimization — more walk-ins and calls from local searches
2. Appointment reminders over WhatsApp — meaningfully fewer no-shows
3. Patient recall — automated 6-month messages that bring lapsed patients back

Your task: produce a concise pre-call brief that Vaibhav will read on his phone (Telegram) right before calling this dental clinic owner.

The brief must be short enough to read in 60 seconds. Use this structure exactly:

**[Clinic name] — Pre-Call Brief**

**Snapshot**
[3 bullet points: their most important signals — review count/rating, biggest GBP gap, website/booking situation]

**What the WhatsApp said**
[1-2 sentences summarizing what we sent and whether/how they replied. If no reply, note that.]

**Open with**
[One specific suggested opening line — not "Hi how are you" but something that references their actual situation]

**Ask them**
[2-3 targeted discovery questions specific to this clinic's profile — not generic]

**Lead with**
[The single Zelva capability most relevant to them, and one sentence on why]

**Likely objection**
[The most probable objection based on their profile, and a 1-sentence counter]

[If a prior call transcript exists:]
**From last call**
[2-3 bullet points of key things they said or committed to]

Keep each section tight — this will be read on a phone screen."""


def generate_call_brief(
    ctx: LeadContext,
    conversation: list[ConversationTurn],
    *,
    client: anthropic.Anthropic,
    model: str = "claude-haiku-4-5-20251001",
    prior_transcript: str | None = None,
) -> str:
    """Return a formatted pre-call brief as a Telegram-ready string."""
    playbook = _load_playbook()
    signal_block = _build_signal_block(ctx)
    convo_block = _build_convo_block(conversation)
    transcript_block = (
        f"\nPrior call transcript:\n{prior_transcript[:2000]}"
        if prior_transcript else ""
    )

    user_prompt = (
        f"Generic call playbook (use as the structural reference):\n{playbook}\n\n"
        f"Clinic signals:\n{signal_block}\n\n"
        f"WhatsApp conversation so far:\n{convo_block}"
        f"{transcript_block}\n\n"
        "Write the pre-call brief."
    )

    try:
        response = client.messages.create(
            model=model,
            max_tokens=800,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:  # noqa: BLE001
        logger.error("call_brief_agent.error clinic={n} err={e}", n=ctx.clinic_name, e=e)
        return _fallback_brief(ctx)


# ── private ───────────────────────────────────────────────────────────

def _load_playbook() -> str:
    try:
        return _CALL_BRIEF_DOC.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "(playbook not found)"


def _build_signal_block(ctx: LeadContext) -> str:
    lines = [
        f"Clinic: {ctx.clinic_name}, {ctx.city}",
    ]
    if ctx.owner_name:
        lines.append(f"Owner: Dr. {ctx.owner_name}")
    if ctx.google_review_count is not None:
        lines.append(f"Google reviews: {ctx.google_review_count} at {ctx.google_rating}★")
    if ctx.review_velocity_30d is not None:
        lines.append(f"New reviews last 30d: {ctx.review_velocity_30d}")
    if ctx.gbp_has_description is not None:
        lines.append(f"GBP description: {'present' if ctx.gbp_has_description else 'MISSING'}")
    if ctx.has_website is False or (ctx.has_website and not ctx.website_loads):
        lines.append("Website: none / not loading (Facebook page only)")
    if ctx.has_online_booking is False:
        lines.append("Online booking: not set up")
    if ctx.has_whatsapp_link is False:
        lines.append("WhatsApp link on profile: missing")
    if ctx.on_practo:
        lines.append("Listed on Practo")
    if ctx.service_mix:
        lines.append(f"Services: {', '.join(ctx.service_mix)}")
    if ctx.pitch_angle:
        lines.append(f"Strongest angle (our scoring): {ctx.pitch_angle}")
    return "\n".join(lines)


def _build_convo_block(conversation: list[ConversationTurn]) -> str:
    if not conversation:
        return "No conversation yet — we have not sent the WhatsApp message."
    lines = []
    for turn in conversation:
        role = "Zelva" if turn.role == "us" else "Lead"
        lines.append(f"{role}: {turn.content}")
    return "\n".join(lines)


def _fallback_brief(ctx: LeadContext) -> str:
    return (
        f"**{ctx.clinic_name} — Pre-Call Brief**\n\n"
        f"City: {ctx.city}\n"
        f"Reviews: {ctx.google_review_count} at {ctx.google_rating}★\n\n"
        f"(AI brief generation failed — refer to the generic call brief doc)\n"
        f"Lead with: {ctx.pitch_angle or 'GBP optimization'}"
    )


__all__ = ["generate_call_brief"]
