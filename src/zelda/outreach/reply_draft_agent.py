"""Reply draft agent — Phase 16e.

When a lead replies to our WhatsApp, this agent reads the full conversation
and drafts a response for Vaibhav to approve on Telegram before it sends.

The agent has full context on:
  - Zelva's capabilities and competitive positioning (from competitive-research.md)
  - The lead's enrichment signals (their specific gaps)
  - The complete WhatsApp conversation thread so far

The draft is WhatsApp-appropriate: short, conversational, advances the
conversation toward a call or trial without being pushy. If the lead is
asking a question, answer it directly. If they're interested, suggest
the 15-minute setup call. If they're objecting, handle it specifically.

Vaibhav sees the draft on Telegram with [Approve] [Edit] [Discard].
It never sends automatically.
"""

from __future__ import annotations

from pathlib import Path

import anthropic
from loguru import logger

from zelda.models.outreach_message import ConversationTurn
from zelda.outreach.whatsapp_personalizer import LeadContext

_COMPETITIVE_RESEARCH_DOC = (
    Path(__file__).parent.parent.parent.parent / "docs" / "competitive-research.md"
)

_SYSTEM_PROMPT_TEMPLATE = """\
You are drafting a WhatsApp reply on behalf of Zelva — a marketing platform built specifically for Indian dental clinics.

About Zelva (use this to answer questions accurately):
{competitive_context}

Zelva's three core capabilities (always accurate to mention):
1. Google Business Profile optimization — more walk-ins and calls from "dentist near me" searches
2. WhatsApp appointment reminders — meaningfully fewer no-shows
3. Patient recall — automated 6-month messages that bring lapsed patients back, without any effort from the doctor

Pricing (only share if directly asked): ₹3,000–5,000/month, depending on the plan. No setup fee.

Your task: draft a WhatsApp reply to the lead's latest message.

Reply guidelines:
- Match the lead's energy: if they're curious, be warm; if they're skeptical, be direct and honest
- If they asked a question, answer it specifically — don't dodge
- If they seem interested, the goal is to get them to agree to a 15-minute setup call or free trial
- If they're objecting (price, time, "already on Practo"), handle the objection specifically using what you know about Zelva
- Keep it SHORT — 3-5 lines maximum, WhatsApp is read on a phone
- No emojis, no corporate language
- Never make promises you can't keep ("guaranteed results", specific patient counts)
- End with a clear next step or question — don't just respond and leave the thread hanging
- Output ONLY the reply text — no preamble, no explanation"""


def draft_reply(
    ctx: LeadContext,
    conversation: list[ConversationTurn],
    *,
    client: anthropic.Anthropic,
    model: str = "claude-haiku-4-5-20251001",
) -> str:
    """Draft a WhatsApp reply for Vaibhav's approval. Returns the draft text."""
    competitive_context = _load_competitive_context()
    system = _SYSTEM_PROMPT_TEMPLATE.format(competitive_context=competitive_context)

    signal_block = _build_signal_block(ctx)
    convo_block = _build_convo_block(conversation)

    user_prompt = (
        f"Clinic signals:\n{signal_block}\n\n"
        f"Full conversation:\n{convo_block}\n\n"
        "Draft a WhatsApp reply to their latest message."
    )

    try:
        response = client.messages.create(
            model=model,
            max_tokens=400,
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:  # noqa: BLE001
        logger.error("reply_draft_agent.error clinic={n} err={e}", n=ctx.clinic_name, e=e)
        return "(AI draft failed — please reply manually)"


# ── private ───────────────────────────────────────────────────────────

def _load_competitive_context() -> str:
    try:
        full = _COMPETITIVE_RESEARCH_DOC.read_text(encoding="utf-8")
        # Keep only the most useful sections — India-specific nuances + value props + gaps
        # to avoid blowing the context window
        keep_sections = [
            "## The Five Value Props",
            "## What Indian Dentists Care About",
            "## India-Specific Nuances",
            "## Market Gaps",
        ]
        lines = full.splitlines()
        result: list[str] = []
        capturing = False
        for line in lines:
            if any(line.startswith(s) for s in keep_sections):
                capturing = True
            elif line.startswith("## ") and capturing:
                capturing = False
            if capturing:
                result.append(line)
        return "\n".join(result) if result else full[:3000]
    except FileNotFoundError:
        return "(competitive research doc not found)"


def _build_signal_block(ctx: LeadContext) -> str:
    lines = [f"Clinic: {ctx.clinic_name}, {ctx.city}"]
    if ctx.owner_name:
        lines.append(f"Owner: Dr. {ctx.owner_name}")
    if ctx.google_review_count is not None:
        lines.append(f"Google: {ctx.google_review_count} reviews, {ctx.google_rating}★")
    if ctx.on_practo:
        lines.append("On Practo: yes")
    if ctx.has_website is False:
        lines.append("Website: none")
    if ctx.has_online_booking is False:
        lines.append("Online booking: not set up")
    if ctx.service_mix:
        lines.append(f"Services: {', '.join(ctx.service_mix)}")
    return "\n".join(lines)


def _build_convo_block(conversation: list[ConversationTurn]) -> str:
    if not conversation:
        return "(no conversation yet)"
    lines = []
    for turn in conversation:
        role = "Zelva" if turn.role == "us" else "Lead"
        lines.append(f"{role}: {turn.content}")
    return "\n".join(lines)


__all__ = ["draft_reply"]
