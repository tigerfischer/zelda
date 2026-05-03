"""Meta WhatsApp Cloud API gateway — send + webhook receive.

Use this instead of GreenAPIGateway when you want an official API channel
(no personal number required — Meta provides a free test number in the
developer sandbox that works immediately).

Quick start (sandbox, no SIM needed):
  1. developers.facebook.com → create App → WhatsApp → Getting Started
  2. Copy "Phone number ID" and the temporary access token
  3. Add to .env:
       WHATSAPP_CLOUD_PHONE_NUMBER_ID=<id>
       WHATSAPP_CLOUD_ACCESS_TOKEN=<token>
  4. The sandbox lets you send to up to 5 pre-approved numbers for free.
     To send to any number you need a verified phone number and a Meta
     Business account (takes ~1 business day to approve).

Receive (replies from WhatsApp users):
  Meta pushes incoming messages to your webhook URL via HTTP POST.
  This gateway starts a local HTTP server that receives those posts.
  You need to expose it with a tunnel (ngrok, cloudflared, etc.):

    ngrok http 8080
    # copy the https:// URL → paste into Meta App Dashboard → Webhooks

  Set in .env:
    WHATSAPP_CLOUD_WEBHOOK_VERIFY_TOKEN=<any random string>
    WHATSAPP_CLOUD_WEBHOOK_PORT=8080  (default)

  If you don't start the server, poll_once() returns [] silently and
  you handle replies manually — fine for V0.

Interface is identical to GreenAPIGateway so the Telegram bot uses
either transparently.
"""

from __future__ import annotations

import json
import queue
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import httpx
from loguru import logger

_GRAPH_BASE = "https://graph.facebook.com/v20.0"
_IST_OFFSET = 5.5 * 3600


class WhatsAppCloudAPIGateway:
    def __init__(
        self,
        phone_number_id: str,
        access_token: str,
        *,
        webhook_verify_token: str = "",
        webhook_port: int = 8080,
    ) -> None:
        if not phone_number_id or not access_token:
            raise ValueError(
                "WHATSAPP_CLOUD_PHONE_NUMBER_ID and WHATSAPP_CLOUD_ACCESS_TOKEN must be set"
            )
        self._phone_number_id = phone_number_id
        self._access_token = access_token
        self._webhook_verify_token = webhook_verify_token
        self._webhook_port = webhook_port
        self._client = httpx.Client(timeout=30)
        self._inbox: queue.Queue[dict] = queue.Queue()
        self._server: HTTPServer | None = None

    def close(self) -> None:
        self._client.close()
        if self._server:
            self._server.shutdown()

    # ── send ─────────────────────────────────────────────────────────

    def send_message(self, phone: str, message: str) -> str:
        """Send a WhatsApp text message. Returns the Meta message ID (wamid.*).

        Raises ValueError outside the allowed send window (9am–7pm IST).
        Raises httpx.HTTPError on API failure.
        """
        if not _is_send_window():
            raise ValueError("Outside send window (9am–7pm IST). Message not sent.")

        url = f"{_GRAPH_BASE}/{self._phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": _to_e164(phone),
            "type": "text",
            "text": {"preview_url": False, "body": message},
        }
        resp = self._client.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {self._access_token}"},
        )
        resp.raise_for_status()
        data = resp.json()
        msg_id = data.get("messages", [{}])[0].get("id", "")
        logger.info("whatsapp_cloud.sent phone={ph} msg_id={mid}", ph=phone, mid=msg_id)
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
        """Start a background HTTP server to receive Meta webhook events.

        Call once after constructing the gateway. The server runs in a daemon
        thread and stops when the process exits.

        You must expose the port externally (e.g. `ngrok http <port>`) and
        configure the resulting HTTPS URL in the Meta App Dashboard under
        WhatsApp → Configuration → Webhook.

        Verification token: whatever you set in WHATSAPP_CLOUD_WEBHOOK_VERIFY_TOKEN.
        Subscribe to the `messages` webhook field.
        """
        inbox = self._inbox
        verify_token = self._webhook_verify_token

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                params = parse_qs(urlparse(self.path).query)
                mode = params.get("hub.mode", [None])[0]
                token = params.get("hub.verify_token", [None])[0]
                challenge = params.get("hub.challenge", [""])[0]
                if mode == "subscribe" and token == verify_token:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(challenge.encode())
                    logger.info("whatsapp_cloud.webhook_verified")
                else:
                    self.send_response(403)
                    self.end_headers()

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")
                for msg in _extract_messages(body):
                    inbox.put(msg)

            def log_message(self, *args: object) -> None:
                pass  # suppress default access log

        server = HTTPServer(("", self._webhook_port), _Handler)
        self._server = server
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info(
            "whatsapp_cloud.webhook_server_started port={p}", p=self._webhook_port
        )


# ── helpers ───────────────────────────────────────────────────────────

def _to_e164(phone: str) -> str:
    """Normalize phone → E.164 digits (no + prefix, just digits)."""
    digits = "".join(c for c in phone if c.isdigit())
    if digits.startswith("0"):
        digits = digits[1:]
    if not digits.startswith("91") and len(digits) == 10:
        digits = "91" + digits
    return digits


def _ist_now() -> datetime:
    from datetime import timedelta
    return datetime.now(timezone.utc) + timedelta(seconds=int(_IST_OFFSET))


def _is_send_window() -> bool:
    t = _ist_now()
    return 9 <= t.hour < 19


def _extract_messages(body: dict) -> list[dict]:
    """Parse a Meta webhook payload into our flat message dicts."""
    results = []
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                if msg.get("type") != "text":
                    continue
                phone = msg.get("from", "")
                text = msg.get("text", {}).get("body", "")
                msg_id = msg.get("id", "")
                if phone and text:
                    results.append(
                        {
                            "phone": phone,
                            "text": text,
                            "msg_id": msg_id,
                            "received_at": datetime.now(timezone.utc).isoformat(),
                        }
                    )
    return results


__all__ = ["WhatsAppCloudAPIGateway"]
