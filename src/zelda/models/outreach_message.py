"""Outreach pipeline state — one row per lead per outreach attempt.

State machine:
    draft
      → pending_review   (loaded into Telegram review queue)
      → approved         (Vaibhav approved on Telegram)
      → sent             (Green API confirmed delivery)
      → call_reminded    (T+2 day Telegram reminder fired)
      → called           (call transcript attached)
    (any state) → skipped

The `conversation` list grows as the WhatsApp thread evolves:
first our message, then any replies, then our reply, etc.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ConversationTurn(BaseModel):
    role: Literal["us", "lead"]
    content: str
    at: datetime


OutreachStatus = Literal[
    "draft",
    "pending_review",
    "approved",
    "sent",
    "call_reminded",
    "called",
    "skipped",
    "no_whatsapp",   # send attempted but number not reachable on WhatsApp
]


class OutreachMessage(BaseModel):
    id: str                              # UUID
    lead_id: str
    clinic_name: str
    city: str
    phone: str

    message: str                         # the WhatsApp text (may be edited before send)
    status: OutreachStatus = "draft"

    created_at: datetime
    approved_at: datetime | None = None
    sent_at: datetime | None = None
    call_reminder_sent_at: datetime | None = None
    called_at: datetime | None = None

    conversation: list[ConversationTurn] = Field(default_factory=list)
    pending_reply_draft: str | None = None   # AI draft waiting for approval
    call_transcript: str | None = None

    # External IDs for correlation
    telegram_review_msg_id: int | None = None  # Telegram message showing the draft
    whatsapp_msg_id: str | None = None          # Green API message ID after send


__all__ = ["OutreachMessage", "ConversationTurn", "OutreachStatus"]
