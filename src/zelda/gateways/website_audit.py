"""Website audit gateway — lightweight HTTP + HTML signal extraction.

Uses `requests` (no browser) so it's fast and cheap. Playwright is not
used here — dental practice websites are mostly server-rendered or have
enough SSR that the initial HTML response carries the signals we need.

Signals extracted:
  - website_loads          — HTTP 200 within timeout
  - is_mobile_friendly     — viewport <meta> present
  - has_schema_markup      — JSON-LD with Dentist / MedicalBusiness / LocalBusiness
  - has_blog               — /blog, /articles, /news, /tips links
  - has_whatsapp_link      — wa.me/ or api.whatsapp.com/send anywhere on page
  - has_online_booking     — booking widget scripts or "book appointment" links
  - has_chat_widget        — live-chat script tags (Tidio, Intercom, Tawk, Freshchat)
  - agency_credit          — footer "designed by / powered by" text + agency name
  - page_text              — visible body text (stripped, for LLM classification)
  - raw_links              — all href values (for booking / blog detection)

All signals are returned as a plain dict — the pass controller decides
which to persist.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse, urljoin

from loguru import logger

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError as e:
    raise ImportError(
        "Website audit requires 'requests' and 'beautifulsoup4'. "
        "Add them to environment.yml."
    ) from e


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_TIMEOUT_S = 10

# ── booking widget detection ───────────────────────────────────────────

# Script src patterns that indicate a booking widget
_BOOKING_SCRIPT_PATTERNS = [
    r"calendly\.com",
    r"simplybook\.",
    r"setmore\.com",
    r"appointy\.com",
    r"vcita\.com",
    r"zocdoc\.com",
    r"practo\.com/widget",
    r"bookingkit\.",
    r"10to8\.com",
    r"acuityscheduling\.com",
    r"doodle\.com",
]

# Link text / href patterns for inline booking CTAs
_BOOKING_LINK_PATTERNS = [
    r"book[\s\-_]?(appointment|now|online|visit|slot)",
    r"schedule[\s\-_]?(appointment|visit|online)",
    r"request[\s\-_]?appointment",
    r"online[\s\-_]?booking",
    r"reserve[\s\-_]?slot",
]

# ── chat widget detection ──────────────────────────────────────────────

_CHAT_SCRIPT_PATTERNS = [
    r"tawk\.to",
    r"tidio\.com",
    r"intercom\.",
    r"freshchat\.",
    r"crisp\.chat",
    r"drift\.com",
    r"zopim\.",
    r"zendesk\.com",
    r"livechat\.com",
    r"smartsupp\.",
]

# ── schema.org type detection ──────────────────────────────────────────

_MEDICAL_SCHEMA_TYPES = {
    "dentist", "dentalpractice", "medicalbusiness",
    "physician", "healthcareprovider", "localbusiness",
}

# ── blog / content detection ───────────────────────────────────────────

_BLOG_PATH_PATTERNS = [
    r"/blog", r"/articles?", r"/news", r"/tips", r"/insights",
    r"/resources", r"/dental-tips", r"/oral-health",
]

# ── agency footer detection ────────────────────────────────────────────

_AGENCY_CREDIT_RE = re.compile(
    r"(designed|developed|powered|built|created|managed)\s+by\s+"
    r"([A-Za-z0-9\s&\.]+?)(?:\s*\||\s*©|\s*<|\s*\n|$)",
    re.IGNORECASE,
)

# ── chain / hospital detection (re-exported for convenience) ──────────

from zelda.controllers.enrichment.chain_detection import (  # noqa: E402
    KNOWN_CHAINS,
    HOSPITAL_KEYWORDS,
    detect_chain,
    detect_hospital,
)


class WebsiteAuditGateway:
    """Fetch and parse a clinic website for enrichment signals."""

    def __init__(self, *, timeout_s: int = _TIMEOUT_S) -> None:
        self._timeout = timeout_s
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "WebsiteAuditGateway":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def audit(self, url: str) -> dict[str, Any]:
        """Fetch `url` and return a dict of extracted signals.

        Always returns a dict — never raises. On network/parse errors,
        `website_loads` is False and `error` carries the message.
        """
        result: dict[str, Any] = {
            "url": url,
            "website_loads": False,
            "is_mobile_friendly": None,
            "has_schema_markup": None,
            "has_blog": None,
            "has_whatsapp_link": None,
            "has_online_booking": None,
            "has_chat_widget": None,
            "agency_credit": None,
            "page_text": "",
            "error": None,
        }

        try:
            resp = self._session.get(url, timeout=self._timeout, allow_redirects=True)
            if resp.status_code >= 400:
                result["error"] = f"HTTP {resp.status_code}"
                return result
            result["website_loads"] = True
            html = resp.text
        except requests.exceptions.Timeout:
            result["error"] = "timeout"
            return result
        except requests.exceptions.ConnectionError as e:
            result["error"] = f"connection_error: {e}"
            return result
        except Exception as e:  # noqa: BLE001
            result["error"] = f"fetch_error: {type(e).__name__}: {e}"
            return result

        try:
            soup = BeautifulSoup(html, "html.parser")
            result.update(self._parse(soup, url, html))
        except Exception as e:  # noqa: BLE001
            logger.warning("website_audit.parse_error url={u} err={e}", u=url, e=e)
            result["error"] = f"parse_error: {e}"

        return result

    # ── private ────────────────────────────────────────────────────────

    def _parse(self, soup: BeautifulSoup, url: str, raw_html: str) -> dict[str, Any]:
        signals: dict[str, Any] = {}

        # Mobile-friendly: viewport meta tag
        viewport = soup.find("meta", attrs={"name": re.compile(r"viewport", re.I)})
        signals["is_mobile_friendly"] = viewport is not None

        # Schema.org markup
        signals["has_schema_markup"] = self._detect_schema(soup)

        # Collect all script src values + inline script text
        script_srcs = [
            (s.get("src") or "") for s in soup.find_all("script")
        ]
        inline_scripts = " ".join(
            (s.string or "") for s in soup.find_all("script") if not s.get("src")
        )
        all_script_text = " ".join(script_srcs) + " " + inline_scripts

        # Online booking
        signals["has_online_booking"] = self._detect_booking(soup, all_script_text)

        # Chat widget
        signals["has_chat_widget"] = self._detect_chat(all_script_text)

        # WhatsApp link
        signals["has_whatsapp_link"] = self._detect_whatsapp(soup, raw_html)

        # Blog / content section
        signals["has_blog"] = self._detect_blog(soup, url)

        # Agency footer credit
        signals["agency_credit"] = self._detect_agency_credit(soup)

        # Clean visible text for LLM classification (capped at 8000 chars)
        signals["page_text"] = self._visible_text(soup)[:8000]

        return signals

    def _detect_schema(self, soup: BeautifulSoup) -> bool:
        import json as _json
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = _json.loads(tag.string or "")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    t = str(item.get("@type", "")).lower().replace(" ", "")
                    if any(m in t for m in _MEDICAL_SCHEMA_TYPES):
                        return True
            except Exception:  # noqa: BLE001
                pass
        return False

    def _detect_booking(self, soup: BeautifulSoup, script_text: str) -> bool:
        # Check script sources
        for pattern in _BOOKING_SCRIPT_PATTERNS:
            if re.search(pattern, script_text, re.IGNORECASE):
                return True

        # Check links and button text
        for a in soup.find_all("a", href=True):
            href = a.get("href", "") or ""
            text = (a.get_text() or "").lower().strip()
            combined = (href + " " + text).lower()
            for pattern in _BOOKING_LINK_PATTERNS:
                if re.search(pattern, combined, re.IGNORECASE):
                    return True

        # Check button text
        for btn in soup.find_all(["button", "input"]):
            text = (btn.get_text() or btn.get("value") or "").lower()
            for pattern in _BOOKING_LINK_PATTERNS:
                if re.search(pattern, text, re.IGNORECASE):
                    return True

        return False

    def _detect_chat(self, script_text: str) -> bool:
        for pattern in _CHAT_SCRIPT_PATTERNS:
            if re.search(pattern, script_text, re.IGNORECASE):
                return True
        return False

    def _detect_whatsapp(self, soup: BeautifulSoup, raw_html: str) -> bool:
        # wa.me links and WhatsApp API links
        wa_pattern = re.compile(
            r'(wa\.me/|api\.whatsapp\.com/send|whatsapp://send)',
            re.IGNORECASE,
        )
        if wa_pattern.search(raw_html):
            return True
        # WhatsApp floating button (common Indian widget pattern)
        for tag in soup.find_all(class_=re.compile(r"whatsapp", re.I)):
            return True
        return False

    def _detect_blog(self, soup: BeautifulSoup, base_url: str) -> bool:
        for a in soup.find_all("a", href=True):
            href = str(a.get("href", "")).lower()
            for pattern in _BLOG_PATH_PATTERNS:
                if re.search(pattern, href):
                    return True
        return False

    def _detect_agency_credit(self, soup: BeautifulSoup) -> str | None:
        footer = soup.find("footer") or soup
        footer_text = footer.get_text(separator=" ", strip=True)
        m = _AGENCY_CREDIT_RE.search(footer_text)
        if m:
            agency = m.group(2).strip()
            if 3 <= len(agency) <= 60:
                return agency
        return None

    def _visible_text(self, soup: BeautifulSoup) -> str:
        # Remove script, style, nav, footer tags from text extraction
        for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
            tag.decompose()
        return " ".join(soup.get_text(separator=" ").split())


__all__ = [
    "WebsiteAuditGateway",
    "KNOWN_CHAINS",
    "detect_chain",
    "detect_hospital",
]
