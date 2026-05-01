"""Green API gateway — WhatsApp send + receive.

Green API is a personal-number WhatsApp gateway (REST-based).
Docs: https://green-api.com/en/docs/

Send: POST /sendMessage
Receive: polling /receiveNotification (no public webhook needed)

Anti-ban posture:
  - Random inter-message delay (30–90s between sends)
  - Messages only sent 9am–7pm IST
  - Never send to the same number more than once without a reply
  - Message body should vary slightly (handled by the personalizer)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx
from loguru import logger


_IST_OFFSET = 5.5 * 3600   # seconds east of UTC


class GreenAPIGateway:
    def __init__(
        self,
        instance_id: str,
        token: str,
        *,
        base_url: str = "https://api.green-api.com",
    ) -> None:
        if not instance_id or not token:
            raise ValueError("GREEN_API_INSTANCE_ID and GREEN_API_TOKEN must be set")
        self._instance_id = instance_id
        self._token = token
        self._base = f"{base_url}/waInstance{instance_id}"
        self._client = httpx.Client(timeout=30)

    def close(self) -> None:
        self._client.close()

    # ── send ─────────────────────────────────────────────────────────

    def send_message(self, phone: str, message: str) -> str:
        """Send a WhatsApp message. Returns the Green API message ID.

        Raises ValueError if outside the allowed send window (9am–7pm IST).
        Raises httpx.HTTPError on API failure.
        """
        if not _is_send_window():
            raise ValueError(
                "Outside send window (9am–7pm IST). Message not sent."
            )
        chat_id = _to_chat_id(phone)
        url = f"{self._base}/sendMessage/{self._token}"
        resp = self._client.post(url, json={"chatId": chat_id, "message": message})
        resp.raise_for_status()
        data = resp.json()
        msg_id = data.get("idMessage", "")
        logger.info(
            "green_api.sent phone={ph} msg_id={mid}", ph=phone, mid=msg_id
        )
        return msg_id

    # ── receive ──────────────────────────────────────────────────────

    def poll_once(self) -> list[dict]:
        """Retrieve up to one pending notification from Green API and delete it.

        Returns a list of parsed message dicts (usually 0 or 1 items).
        Call in a loop from the Telegram bot's background task.
        """
        url = f"{self._base}/receiveNotification/{self._token}"
        try:
            resp = self._client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("green_api.poll_error err={e}", e=e)
            return []

        data = resp.json()
        if not data:
            return []

        receipt_id = data.get("receiptId")
        body = data.get("body", {})

        # Delete the notification so it's not returned again
        if receipt_id:
            self._delete_notification(receipt_id)

        msg = _parse_notification(body)
        return [msg] if msg else []

    def _delete_notification(self, receipt_id: int) -> None:
        url = f"{self._base}/deleteNotification/{self._token}/{receipt_id}"
        try:
            self._client.delete(url)
        except httpx.HTTPError as e:
            logger.warning(
                "green_api.delete_notification_error receipt={r} err={e}",
                r=receipt_id, e=e,
            )


# ── helpers ───────────────────────────────────────────────────────────

def _to_chat_id(phone: str) -> str:
    """Normalize phone → Green API chatId format (91XXXXXXXXXX@c.us)."""
    digits = "".join(c for c in phone if c.isdigit())
    # Strip leading 0 (Indian local format), add country code if needed
    if digits.startswith("0"):
        digits = digits[1:]
    if not digits.startswith("91") and len(digits) == 10:
        digits = "91" + digits
    return f"{digits}@c.us"


def _is_send_window() -> bool:
    """True if current IST time is between 09:00 and 19:00."""
    utc_now = datetime.now(timezone.utc)
    ist_seconds = (utc_now.hour * 3600 + utc_now.minute * 60 + utc_now.second + _IST_OFFSET) % 86400
    ist_hour = ist_seconds / 3600
    return 9.0 <= ist_hour < 19.0


def _parse_notification(body: dict) -> dict | None:
    """Extract the fields we care about from a Green API notification body."""
    msg_type = body.get("typeWebhook")
    if msg_type != "incomingMessageReceived":
        return None

    sender_data = body.get("senderData", {})
    msg_data = body.get("messageData", {})
    text_data = msg_data.get("textMessageData", {})

    phone_raw = sender_data.get("sender", "")          # e.g. "919876543210@c.us"
    phone = phone_raw.split("@")[0]
    text = text_data.get("textMessage", "")
    msg_id = body.get("idMessage", "")

    if not phone or not text:
        return None

    return {
        "phone": phone,
        "text": text,
        "msg_id": msg_id,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }


__all__ = ["GreenAPIGateway"]
