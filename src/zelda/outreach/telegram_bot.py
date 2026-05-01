"""Telegram bot — the human-in-the-loop interface for the outreach pipeline.

Run with:
    python -m zelda telegram-bot

Setup (one-time):
    1. Message @BotFather on Telegram → /newbot → copy the token
    2. Set TELEGRAM_BOT_TOKEN=<token> in .env
    3. Set TELEGRAM_CHAT_ID=<your chat ID> in .env
       (send /start to the bot, then GET https://api.telegram.org/bot<TOKEN>/getUpdates
        to find your chat ID in the response)
    4. Set GREEN_API_INSTANCE_ID and GREEN_API_TOKEN in .env

The bot does three things on a continuous loop:

  1. Message review queue
     Sends unreviewed WhatsApp drafts to Telegram with [Approve] [Edit] [Skip].
     Approve → marks approved + sends via Green API immediately.
     Edit → bot asks for new text, shows it again for re-approval.
     Skip → marks skipped, removes from queue.

  2. Call reminders (checked every 5 minutes)
     Finds leads where sent_at > 2 days ago and call_reminder_sent_at IS NULL.
     Generates a personalized call brief via CallBriefAgent.
     Posts to Telegram. Marks call_reminder_sent_at.

  3. Reply alerts (polled from Green API every 15 seconds)
     Incoming WhatsApp replies are matched to outreach records by phone number.
     ReplyDraftAgent generates a draft reply.
     Posted to Telegram with [Approve] [Edit] [Discard].
     On Approve → sends via Green API.

Requires: python-telegram-bot>=20.0 (async PTB)
Install: conda install -c conda-forge python-telegram-bot
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from zelda.config import Settings


async def run_bot(settings: "Settings") -> None:
    """Entry point — starts the Telegram bot and all background tasks."""
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
        from telegram.ext import (
            Application,
            CallbackQueryHandler,
            CommandHandler,
            ContextTypes,
            MessageHandler,
            filters,
        )
    except ImportError:
        raise SystemExit(
            "python-telegram-bot is not installed.\n"
            "Run: conda install -c conda-forge python-telegram-bot"
        )

    import anthropic
    from zelda.gateways.google_drive import GoogleDriveGateway
    from zelda.gateways.green_api import GreenAPIGateway
    from zelda.outreach.call_brief_agent import generate_call_brief
    from zelda.outreach.drive_recording_sync import DriveRecordingSync
    from zelda.outreach.reply_draft_agent import draft_reply
    from zelda.outreach.whatsapp_personalizer import lead_context_from_enrichment
    from zelda.repositories.lead_enrichment_repo import LeadEnrichmentRepository
    from zelda.repositories.outreach_repo import OutreachRepository

    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set in .env")
    if not settings.telegram_chat_id:
        raise SystemExit("TELEGRAM_CHAT_ID is not set in .env")

    chat_id = int(settings.telegram_chat_id)
    outreach_repo = OutreachRepository(settings.db_path)
    enrichment_repo = LeadEnrichmentRepository(settings.db_path)
    anthropic_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    green_api: GreenAPIGateway | None = None
    if settings.green_api_instance_id and settings.green_api_token:
        green_api = GreenAPIGateway(
            settings.green_api_instance_id, settings.green_api_token
        )

    drive_sync: DriveRecordingSync | None = None
    if settings.google_drive_folder_id:
        drive = GoogleDriveGateway.from_oauth_file(
            settings.google_oauth_client_secrets,
            settings.google_oauth_token_cache,
            settings.google_drive_folder_id,
        )
        audio_dir = settings.data_dir / "call-recordings"
        audio_dir.mkdir(parents=True, exist_ok=True)
        drive_sync = DriveRecordingSync(drive, outreach_repo, audio_dir)

    # ── state for multi-step edit flow ─────────────────────────────
    # pending_edits: {telegram_msg_id: outreach_id} — waiting for user to send new text
    pending_edits: dict[int, str] = {}
    # pending_reply_approvals: {telegram_msg_id: outreach_id}
    pending_reply_approvals: dict[int, str] = {}

    # ── handlers ───────────────────────────────────────────────────

    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        queue = outreach_repo.get_pending_review()
        reply_queue = outreach_repo.get_with_pending_reply()
        await update.message.reply_text(  # type: ignore[union-attr]
            f"Outreach queue: {len(queue)} drafts pending review\n"
            f"Reply drafts waiting approval: {len(reply_queue)}"
        )

    async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        data = query.data or ""

        if data.startswith("approve:"):
            await _handle_approve(query, data[8:], context)
        elif data.startswith("edit:"):
            await _handle_edit_request(query, data[5:], pending_edits, context)
        elif data.startswith("skip:"):
            await _handle_skip(query, data[5:], context)
        elif data.startswith("approve_reply:"):
            await _handle_approve_reply(query, data[14:], context)
        elif data.startswith("discard_reply:"):
            await _handle_discard_reply(query, data[14:], context)

    async def _handle_approve(query, outreach_id: str, context) -> None:
        msg = outreach_repo.get(outreach_id)
        if not msg:
            await query.edit_message_text("Message not found.")
            return

        if green_api:
            try:
                wa_id = green_api.send_message(msg.phone, msg.message)
                now = datetime.now(timezone.utc)
                outreach_repo.set_status(
                    outreach_id, "sent",
                    sent_at=now, approved_at=now,
                )
                outreach_repo.upsert(
                    outreach_repo.get(outreach_id).__class__(
                        **{**outreach_repo.get(outreach_id).model_dump(), "whatsapp_msg_id": wa_id}
                    )
                )
                await query.edit_message_text(
                    f"Sent to {msg.clinic_name} ({msg.phone})"
                )
            except ValueError as e:
                await query.edit_message_text(f"Not sent: {e}")
            except Exception as e:  # noqa: BLE001
                await query.edit_message_text(f"Send failed: {e}")
        else:
            outreach_repo.set_status(outreach_id, "approved", approved_at=datetime.now(timezone.utc))
            await query.edit_message_text(
                f"Approved (Green API not configured — message queued for manual send)"
            )

    async def _handle_edit_request(query, outreach_id: str, pending: dict, context) -> None:
        msg = await query.message.reply_text(  # type: ignore[union-attr]
            "Send me the revised message text:"
        )
        pending[msg.message_id] = outreach_id  # type: ignore[union-attr]

    async def _handle_skip(query, outreach_id: str, context) -> None:
        outreach_repo.set_status(outreach_id, "skipped")
        await query.edit_message_text("Skipped.")

    async def _handle_approve_reply(query, outreach_id: str, context) -> None:
        msg = outreach_repo.get(outreach_id)
        if not msg or not msg.pending_reply_draft:
            await query.edit_message_text("Draft not found.")
            return

        if green_api:
            try:
                green_api.send_message(msg.phone, msg.pending_reply_draft)
                from zelda.models.outreach_message import ConversationTurn
                outreach_repo.add_conversation_turn(
                    outreach_id,
                    ConversationTurn(role="us", content=msg.pending_reply_draft, at=datetime.now(timezone.utc)),
                )
                outreach_repo.set_pending_reply_draft(outreach_id, None)
                await query.edit_message_text("Reply sent.")
            except Exception as e:  # noqa: BLE001
                await query.edit_message_text(f"Send failed: {e}")
        else:
            await query.edit_message_text("Reply approved (Green API not configured).")

    async def _handle_discard_reply(query, outreach_id: str, context) -> None:
        outreach_repo.set_pending_reply_draft(outreach_id, None)
        await query.edit_message_text("Reply draft discarded.")

    async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle free-text replies — used for the edit flow."""
        msg_id = update.message.reply_to_message.message_id if update.message.reply_to_message else None  # type: ignore[union-attr]

        if msg_id and msg_id in pending_edits:
            outreach_id = pending_edits.pop(msg_id)
            new_text = update.message.text or ""  # type: ignore[union-attr]
            record = outreach_repo.get(outreach_id)
            if record:
                record = record.model_copy(update={"message": new_text})
                outreach_repo.upsert(record)
                await _send_for_review(update.get_bot(), chat_id, record, outreach_repo)
                await update.message.reply_text("Updated. Review the new version above.")  # type: ignore[union-attr]

    # ── background tasks ───────────────────────────────────────────

    async def push_review_queue(app) -> None:
        """Send all pending_review drafts to Telegram."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        queue = outreach_repo.get_pending_review()
        for msg in queue:
            await _send_for_review(app.bot, chat_id, msg, outreach_repo)

    async def check_call_reminders(app) -> None:
        """Fire call reminder briefs for leads that hit the T+2 day window."""
        due = outreach_repo.get_due_call_reminders(older_than_days=2.0)
        for msg in due:
            enrichment = enrichment_repo.get(msg.lead_id)
            if not enrichment:
                continue
            from zelda.outreach.whatsapp_personalizer import lead_context_from_enrichment
            ctx = lead_context_from_enrichment(enrichment)
            brief = generate_call_brief(
                ctx, msg.conversation,
                client=anthropic_client,
                prior_transcript=msg.call_transcript,
            )
            await app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"CALL REMINDER — {msg.clinic_name}\n"
                    f"Phone: {msg.phone}\n\n"
                    f"{brief}"
                ),
                parse_mode="Markdown",
            )
            outreach_repo.set_status(
                msg.id, "call_reminded",
                call_reminder_sent_at=datetime.now(timezone.utc),
            )
            logger.info("telegram_bot.call_reminder_sent lead={lid}", lid=msg.lead_id)

    async def poll_whatsapp_replies(app) -> None:
        """Poll Green API for incoming messages and dispatch reply drafts."""
        if not green_api:
            return

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from zelda.models.outreach_message import ConversationTurn

        notifications = green_api.poll_once()
        for notif in notifications:
            phone = notif["phone"]
            text = notif["text"]

            # Match to an outreach record by phone number
            # Simple linear scan — small dataset, acceptable
            # (In future: add a phone index to outreach_messages table)
            matched = None
            for record in outreach_repo.list_for_city(""):  # all cities
                if _normalize_phone(record.phone) == _normalize_phone(phone):
                    matched = record
                    break

            if not matched:
                logger.warning("telegram_bot.unmatched_reply phone={ph}", ph=phone)
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=f"Unmatched WhatsApp reply from {phone}:\n\n{text}",
                )
                continue

            # Record the incoming turn
            turn = ConversationTurn(role="lead", content=text, at=datetime.now(timezone.utc))
            outreach_repo.add_conversation_turn(matched.id, turn)
            updated = outreach_repo.get(matched.id)

            # Generate reply draft
            enrichment = enrichment_repo.get(matched.lead_id)
            if enrichment:
                ctx = lead_context_from_enrichment(enrichment)
                draft = draft_reply(ctx, updated.conversation, client=anthropic_client)  # type: ignore[union-attr]
            else:
                draft = "(no enrichment — draft manually)"

            outreach_repo.set_pending_reply_draft(matched.id, draft)

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Send reply", callback_data=f"approve_reply:{matched.id}"),
                    InlineKeyboardButton("Discard", callback_data=f"discard_reply:{matched.id}"),
                ]
            ])
            await app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"Reply from {matched.clinic_name} ({phone}):\n\n"
                    f'"{text}"\n\n'
                    f"Suggested reply:\n{draft}"
                ),
                reply_markup=keyboard,
            )

    async def sync_drive_recordings(app) -> None:
        if drive_sync:
            try:
                drive_sync.sync()
            except Exception as e:  # noqa: BLE001
                logger.error("telegram_bot.drive_sync_error err={e}", e=e)

    # ── periodic job wrapper ────────────────────────────────────────

    async def _periodic(app, fn, interval_s: int) -> None:
        while True:
            try:
                await fn(app)
            except Exception as e:  # noqa: BLE001
                logger.error("telegram_bot.periodic_error fn={fn} err={e}", fn=fn.__name__, e=e)
            await asyncio.sleep(interval_s)

    # ── build and start the app ────────────────────────────────────

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

    app = Application.builder().token(settings.telegram_bot_token).build()

    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    # Push any already-queued drafts immediately on startup
    await push_review_queue(app)

    # Background loops
    await asyncio.gather(
        _periodic(app, check_call_reminders, 300),       # every 5 min
        _periodic(app, poll_whatsapp_replies, 15),        # every 15 sec
        _periodic(app, sync_drive_recordings, 600),       # every 10 min
    )


# ── helper ────────────────────────────────────────────────────────────

async def _send_for_review(bot, chat_id: int, msg, outreach_repo) -> None:
    """Send a draft WhatsApp message to Telegram for approval."""
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except ImportError:
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Approve & Send", callback_data=f"approve:{msg.id}"),
            InlineKeyboardButton("Edit", callback_data=f"edit:{msg.id}"),
            InlineKeyboardButton("Skip", callback_data=f"skip:{msg.id}"),
        ]
    ])
    tg_msg = await bot.send_message(
        chat_id=chat_id,
        text=(
            f"WhatsApp draft for {msg.clinic_name}\n"
            f"Phone: {msg.phone}\n"
            f"City: {msg.city}\n\n"
            f"{msg.message}"
        ),
        reply_markup=keyboard,
    )
    # Record the Telegram message ID so we can edit it later if needed
    updated = msg.model_copy(update={"telegram_review_msg_id": tg_msg.message_id})
    outreach_repo.upsert(updated)


def _normalize_phone(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    if digits.startswith("91") and len(digits) == 12:
        return digits
    if len(digits) == 10:
        return "91" + digits
    return digits


__all__ = ["run_bot"]
