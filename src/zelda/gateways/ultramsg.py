"""UltraMsg WhatsApp gateway — send + webhook receive.

UltraMsg is a QR-based personal WhatsApp gateway (REST, form-encoded).
Unlike Green API it allows sending free-form messages to any number —
no session 466 issues.

Setup (one-time, ~10 min):
  1. ultramsg.com → Sign up → Create instance
  2. Scan QR with your WhatsApp (Settings → Linked Devices → Link a Device)
  3. Status should show "Connected"
  4. Copy Instance ID and Token from the dashboard
  5. Add to .env:
       ULTRAMSG_INSTANCE_ID=<id>       (e.g. instance12345)
       ULTRAMSG_TOKEN=<token>

Receive (optional — for reply detection):
  UltraMsg is webhook-only for incoming messages. Expose your local port
  with ngrok and paste the URL in the instance dashboard → Webhooks.

  Set in .env:
    ULTRAMSG_WEBHOOK_PORT=8081   (default, change if 8081 is in use)

  If you don't start the webhook server, poll_once() returns [] silently
  and you handle replies manually — fine for V0.
"""

from __future__ import annotations

import json
import queue
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from loguru import logger

_IST_OFFSET = 5.5 * 3600


class UltraMsgGateway:
    def __init__(
        self,
        instance_id: str,
        token: str,
        *,
        webhook_port: int = 8081,
    ) -> None:
        if not instance_id or not token:
            raise ValueError("ULTRAMSG_INSTANCE_ID and ULTRAMSG_TOKEN must be set")
        self._instance_id = instance_id
        self._token = token
        self._base = f"https://api.ultramsg.com/{instance_id}"
        self._webhook_port = webhook_port
        self._client = httpx.Client(timeout=30)
        self._inbox: queue.Queue[dict] = queue.Queue()
        self._server: HTTPServer | None = None

    def close(self) -> None:
        self._client.close()
        if self._server:
            self._server.shutdown()

    # ── send ─────────────────────────────────────────────────────────

    def is_on_whatsapp(self, phone: str) -> bool:
        """Return True if the number is registered on WhatsApp.

        Uses UltraMsg's contacts/check endpoint. Returns True on any API error
        so a network blip doesn't silently drop messages — the send will then
        attempt and fail with its own error if the number truly isn't there.
        """
        try:
            resp = self._client.get(
                f"{self._base}/contacts/check",
                params={"token": self._token, "chatId": _to_chat_id(phone)},
            )
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status", "")
            on_wa = status == "valid"
            logger.info(
                "ultramsg.check phone={ph} status={s} on_whatsapp={w}",
                ph=phone, s=status, w=on_wa,
            )
            return on_wa
        except Exception as e:  # noqa: BLE001
            logger.warning("ultramsg.check_error phone={ph} err={e} — assuming valid", ph=phone, e=e)
            return True  # fail open: let the send attempt proceed

    def send_message(self, phone: str, message: str) -> str:
        """Send a WhatsApp text message. Returns the UltraMsg message ID.

        Raises ValueError if the number is not on WhatsApp or outside the
        allowed send window (9am–7pm IST). Raises httpx.HTTPError on API failure.
        """
        if not _is_send_window():
            raise ValueError("Outside send window (9am–7pm IST). Message not sent.")

        if not self.is_on_whatsapp(phone):
            raise ValueError(f"Number {phone} is not registered on WhatsApp.")

        resp = self._client.post(
            f"{self._base}/messages/chat",
            data={
                "token": self._token,
                "to": _to_chat_id(phone),
                "body": message,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        msg_id = data.get("id", "")
        logger.info("ultramsg.sent phone={ph} msg_id={mid}", ph=phone, mid=msg_id)
        return msg_id

    # ── receive ──────────────────────────────────────────────────────

    def poll_once(self) -> list[dict]:
        """Drain incoming messages from the webhook queue.

        Returns parsed message dicts in the same format as GreenAPIGateway.poll_once().
        Always returns [] if start_webhook_server() has not been called.
        """
        results = []
        while not self._inbox.empty():
            try:
                results.append(self._inbox.get_nowait())
            except queue.Empty:
                break
        return results

    def start_webhook_server(self) -> None:
        """Start a background HTTP server to receive UltraMsg webhook events.

        Expose the port externally (e.g. `ngrok http 8081`) and paste the
        HTTPS URL into the UltraMsg instance dashboard → Webhooks.
        """
        inbox = self._inbox

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")
                msg = _parse_webhook(body)
                if msg:
                    inbox.put(msg)

            def log_message(self, *args: object) -> None:
                pass

        server = HTTPServer(("", self._webhook_port), _Handler)
        self._server = server
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info("ultramsg.webhook_server_started port={p}", p=self._webhook_port)


# ── helpers ───────────────────────────────────────────────────────────

def _to_chat_id(phone: str) -> str:
    """Normalize phone → UltraMsg chatId format (91XXXXXXXXXX@c.us)."""
    digits = "".join(c for c in phone if c.isdigit())
    if digits.startswith("0"):
        digits = digits[1:]
    if not digits.startswith("91") and len(digits) == 10:
        digits = "91" + digits
    return f"{digits}@c.us"


def _ist_now() -> datetime:
    from datetime import timedelta
    return datetime.now(timezone.utc) + timedelta(seconds=int(_IST_OFFSET))


def _is_send_window() -> bool:
    t = _ist_now()
    return 9 <= t.hour < 19


def _parse_webhook(body: dict) -> dict | None:
    """Parse a UltraMsg webhook payload into our flat message dict."""
    if body.get("event_type") != "message_received":
        return None
    phone_raw = body.get("from", "")          # e.g. "919876543210@c.us"
    phone = phone_raw.split("@")[0]
    text = body.get("body", "")
    msg_id = body.get("id", "")
    if not phone or not text:
        return None
    return {
        "phone": phone,
        "text": text,
        "msg_id": msg_id,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }


__all__ = ["UltraMsgGateway"]
