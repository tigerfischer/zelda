"""SQLite repository for the outreach pipeline state.

One row per lead per outreach attempt. The conversation column stores
a JSON array of {role, content, at} objects that grows as the WhatsApp
thread evolves.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from zelda.models.outreach_message import ConversationTurn, OutreachMessage, OutreachStatus

_EDITS_SCHEMA = """
CREATE TABLE IF NOT EXISTS message_edits (
    id               TEXT PRIMARY KEY,
    outreach_id      TEXT NOT NULL,
    lead_id          TEXT NOT NULL,
    clinic_name      TEXT NOT NULL,
    original_message TEXT NOT NULL,
    instruction      TEXT NOT NULL,
    revised_message  TEXT NOT NULL,
    created_at       TEXT NOT NULL
)
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outreach_messages (
    id                      TEXT PRIMARY KEY,
    lead_id                 TEXT NOT NULL,
    clinic_name             TEXT NOT NULL,
    city                    TEXT NOT NULL,
    phone                   TEXT,
    message                 TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'draft',
    created_at              TEXT NOT NULL,
    approved_at             TEXT,
    sent_at                 TEXT,
    call_reminder_sent_at   TEXT,
    called_at               TEXT,
    conversation            TEXT NOT NULL DEFAULT '[]',
    pending_reply_draft     TEXT,
    call_transcript         TEXT,
    telegram_review_msg_id  INTEGER,
    whatsapp_msg_id         TEXT
)
"""


class OutreachRepository:
    def __init__(self, db_path: Path | str) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)
        self._conn.execute(_EDITS_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ── writes ───────────────────────────────────────────────────────

    def upsert(self, msg: OutreachMessage) -> None:
        """Insert or replace the full message record."""
        self._conn.execute(
            """
            INSERT INTO outreach_messages
              (id, lead_id, clinic_name, city, phone, message, status,
               created_at, approved_at, sent_at, call_reminder_sent_at, called_at,
               conversation, pending_reply_draft, call_transcript,
               telegram_review_msg_id, whatsapp_msg_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              message                 = excluded.message,
              status                  = excluded.status,
              approved_at             = excluded.approved_at,
              sent_at                 = excluded.sent_at,
              call_reminder_sent_at   = excluded.call_reminder_sent_at,
              called_at               = excluded.called_at,
              conversation            = excluded.conversation,
              pending_reply_draft     = excluded.pending_reply_draft,
              call_transcript         = excluded.call_transcript,
              telegram_review_msg_id  = excluded.telegram_review_msg_id,
              whatsapp_msg_id         = excluded.whatsapp_msg_id
            """,
            _to_row(msg),
        )
        self._conn.commit()

    def set_status(self, msg_id: str, status: OutreachStatus, **timestamps: datetime | None) -> None:
        """Update status + any timestamp fields atomically."""
        sets = ["status = ?"]
        params: list[Any] = [status]
        for col, val in timestamps.items():
            sets.append(f"{col} = ?")
            params.append(val.isoformat() if val else None)
        params.append(msg_id)
        self._conn.execute(
            f"UPDATE outreach_messages SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        self._conn.commit()

    def add_conversation_turn(self, msg_id: str, turn: ConversationTurn) -> None:
        row = self._conn.execute(
            "SELECT conversation FROM outreach_messages WHERE id = ?", [msg_id]
        ).fetchone()
        if not row:
            logger.warning("outreach_repo.add_turn.not_found id={id}", id=msg_id)
            return
        turns = json.loads(row["conversation"])
        turns.append(turn.model_dump(mode="json"))
        self._conn.execute(
            "UPDATE outreach_messages SET conversation = ? WHERE id = ?",
            [json.dumps(turns), msg_id],
        )
        self._conn.commit()

    def set_pending_reply_draft(self, msg_id: str, draft: str | None) -> None:
        self._conn.execute(
            "UPDATE outreach_messages SET pending_reply_draft = ? WHERE id = ?",
            [draft, msg_id],
        )
        self._conn.commit()

    def log_edit(
        self,
        outreach_id: str,
        lead_id: str,
        clinic_name: str,
        original_message: str,
        instruction: str,
        revised_message: str,
    ) -> None:
        """Record one human-requested rewrite for later agent training."""
        self._conn.execute(
            """
            INSERT INTO message_edits
              (id, outreach_id, lead_id, clinic_name, original_message, instruction, revised_message, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                str(uuid.uuid4()),
                outreach_id,
                lead_id,
                clinic_name,
                original_message,
                instruction,
                revised_message,
                datetime.now(timezone.utc).isoformat(),
            ],
        )
        self._conn.commit()

    def set_call_transcript(self, msg_id: str, transcript: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE outreach_messages SET call_transcript = ?, called_at = ? WHERE id = ?",
            [transcript, now, msg_id],
        )
        self._conn.commit()

    # ── reads ────────────────────────────────────────────────────────

    def get(self, msg_id: str) -> OutreachMessage | None:
        row = self._conn.execute(
            "SELECT * FROM outreach_messages WHERE id = ?", [msg_id]
        ).fetchone()
        return _from_row(row) if row else None

    def get_by_lead(self, lead_id: str) -> OutreachMessage | None:
        """Most recent outreach record for a lead."""
        row = self._conn.execute(
            "SELECT * FROM outreach_messages WHERE lead_id = ? ORDER BY created_at DESC LIMIT 1",
            [lead_id],
        ).fetchone()
        return _from_row(row) if row else None

    def get_pending_review(self) -> list[OutreachMessage]:
        """All messages awaiting Telegram approval."""
        rows = self._conn.execute(
            "SELECT * FROM outreach_messages WHERE status = 'pending_review' ORDER BY created_at"
        ).fetchall()
        return [_from_row(r) for r in rows]

    def get_pending_review_unpushed(self, limit: int | None = None) -> list[OutreachMessage]:
        """Pending-review messages not yet sent to Telegram (telegram_review_msg_id IS NULL)."""
        sql = (
            "SELECT * FROM outreach_messages "
            "WHERE status = 'pending_review' AND telegram_review_msg_id IS NULL "
            "ORDER BY created_at"
        )
        if limit is not None:
            sql += f" LIMIT {limit}"
        rows = self._conn.execute(sql).fetchall()
        return [_from_row(r) for r in rows]

    def count_pushed_pending_review(self) -> int:
        """Messages already visible in Telegram but not yet approved/skipped."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM outreach_messages "
            "WHERE status = 'pending_review' AND telegram_review_msg_id IS NOT NULL"
        ).fetchone()
        return row[0] if row else 0

    def get_due_call_reminders(self, *, older_than_days: float = 2.0) -> list[OutreachMessage]:
        """Sent messages where the call reminder hasn't fired yet and T+N days have elapsed."""
        cutoff = datetime.now(timezone.utc)
        rows = self._conn.execute(
            """
            SELECT * FROM outreach_messages
            WHERE status IN ('sent', 'call_reminded')
              AND sent_at IS NOT NULL
              AND call_reminder_sent_at IS NULL
              AND julianday('now') - julianday(sent_at) >= ?
            ORDER BY sent_at
            """,
            [older_than_days],
        ).fetchall()
        return [_from_row(r) for r in rows]

    def get_with_pending_reply(self) -> list[OutreachMessage]:
        """Messages that have an AI draft reply waiting for Telegram approval."""
        rows = self._conn.execute(
            "SELECT * FROM outreach_messages WHERE pending_reply_draft IS NOT NULL ORDER BY created_at"
        ).fetchall()
        return [_from_row(r) for r in rows]

    def get_approved_unsent(self) -> list[OutreachMessage]:
        """Messages approved but not yet sent — waiting for the dispatch window."""
        rows = self._conn.execute(
            "SELECT * FROM outreach_messages WHERE status = 'approved' ORDER BY approved_at"
        ).fetchall()
        return [_from_row(r) for r in rows]

    def count_initial_sent_today(self) -> int:
        """Initial outreach messages sent today (IST date).

        Uses UTC date shifted by +5:30 so the count rolls over at IST midnight,
        not UTC midnight — avoids crediting yesterday's sends to today's quota.
        """
        row = self._conn.execute(
            """
            SELECT COUNT(*) FROM outreach_messages
            WHERE status IN ('sent', 'call_reminded', 'called')
              AND sent_at IS NOT NULL
              AND date(datetime(sent_at, '+5 hours', '30 minutes')) =
                  date(datetime('now', '+5 hours', '30 minutes'))
            """
        ).fetchone()
        return row[0] if row else 0

    def list_for_city(self, city: str) -> list[OutreachMessage]:
        rows = self._conn.execute(
            "SELECT * FROM outreach_messages WHERE city = ? ORDER BY created_at DESC",
            [city],
        ).fetchall()
        return [_from_row(r) for r in rows]


# ── private ───────────────────────────────────────────────────────────

def _to_row(msg: OutreachMessage) -> tuple:
    return (
        msg.id,
        msg.lead_id,
        msg.clinic_name,
        msg.city,
        msg.phone,
        msg.message,
        msg.status,
        msg.created_at.isoformat(),
        msg.approved_at.isoformat() if msg.approved_at else None,
        msg.sent_at.isoformat() if msg.sent_at else None,
        msg.call_reminder_sent_at.isoformat() if msg.call_reminder_sent_at else None,
        msg.called_at.isoformat() if msg.called_at else None,
        json.dumps([t.model_dump(mode="json") for t in msg.conversation]),
        msg.pending_reply_draft,
        msg.call_transcript,
        msg.telegram_review_msg_id,
        msg.whatsapp_msg_id,
    )


def _from_row(row: sqlite3.Row) -> OutreachMessage:
    turns_raw = json.loads(row["conversation"] or "[]")
    turns = [ConversationTurn(**t) for t in turns_raw]
    return OutreachMessage(
        id=row["id"],
        lead_id=row["lead_id"],
        clinic_name=row["clinic_name"],
        city=row["city"],
        phone=row["phone"] or "",
        message=row["message"],
        status=row["status"],
        created_at=datetime.fromisoformat(row["created_at"]),
        approved_at=datetime.fromisoformat(row["approved_at"]) if row["approved_at"] else None,
        sent_at=datetime.fromisoformat(row["sent_at"]) if row["sent_at"] else None,
        call_reminder_sent_at=datetime.fromisoformat(row["call_reminder_sent_at"]) if row["call_reminder_sent_at"] else None,
        called_at=datetime.fromisoformat(row["called_at"]) if row["called_at"] else None,
        conversation=turns,
        pending_reply_draft=row["pending_reply_draft"],
        call_transcript=row["call_transcript"],
        telegram_review_msg_id=row["telegram_review_msg_id"],
        whatsapp_msg_id=row["whatsapp_msg_id"],
    )


__all__ = ["OutreachRepository"]
