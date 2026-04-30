"""Shared Playwright plumbing for Practo gateways.

Both the per-profile enrichment gateway (`practo_playwright`) and the
URL-discovery search gateway (`practo_search`) need an identical
Chromium setup to clear Practo's Akamai layer:

- Chrome's modern headless mode (`--headless=new`) — uses the same
  rendering pipeline as headful Chrome and is therefore
  indistinguishable to fingerprinting.
- `--disable-blink-features=AutomationControlled` — hides the CDP
  "automation" marker that JS challenges look for.
- A context-init script that nulls `navigator.webdriver` — covers the
  remaining easily-detected automation signal.
- A realistic Mac/Chrome User-Agent and a desktop-sized viewport.

Anything more (residential proxies, mouse-movement emulation, IP
rotation) hasn't been needed in our testing — Practo's Akamai
deployment is on the lighter end. If we get challenged from a cloud
deployment later, those would be the next dials.

This module is intentionally a leading-underscore name so it stays
gateway-internal — the public surface is the gateway classes
themselves.
"""

from __future__ import annotations

import random
import time

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Playwright,
    sync_playwright,
)


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

DEFAULT_VIEWPORT: tuple[int, int] = (1366, 900)

# Akamai's bot-challenge interstitial uses this exact title.
CHALLENGE_TITLE = "Challenge Validation"

# Stealth init script — runs in every new page's context before any
# page script. The single property override is enough for Practo;
# heavier shims (canvas spoofing, etc.) aren't required here.
STEALTH_INIT_SCRIPT = (
    'Object.defineProperty(navigator, "webdriver", {get: () => undefined});'
)

# Default Chrome launch args. `--no-sandbox` is needed when running in
# certain container environments and is harmless on macOS.
CHROMIUM_LAUNCH_ARGS: tuple[str, ...] = (
    "--disable-blink-features=AutomationControlled",
    "--headless=new",
    "--no-sandbox",
)


def launch_chromium() -> tuple[Playwright, Browser]:
    """Spawn a Playwright + Chromium pair tuned for Practo's Akamai.

    `headless=False` is intentional — we add `--headless=new` to the
    Chrome args ourselves. That combination uses Chrome's modern
    headless mode (which shares its rendering pipeline with headful
    Chrome) instead of the legacy `headless_shell` binary that
    `headless=True` ships, which IS detectable.
    """
    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=False,
        args=list(CHROMIUM_LAUNCH_ARGS),
    )
    return pw, browser


def make_context(
    browser: Browser,
    *,
    user_agent: str | None = None,
    viewport: tuple[int, int] | None = None,
) -> BrowserContext:
    """Create a fresh BrowserContext with the stealth init script applied.

    Caller decides cadence — typically a fresh context per-N-fetches
    to flush accumulated cookies / fingerprint signals.
    """
    width, height = viewport or DEFAULT_VIEWPORT
    ctx = browser.new_context(
        user_agent=user_agent or DEFAULT_USER_AGENT,
        viewport={"width": width, "height": height},
        locale="en-US",
    )
    ctx.add_init_script(STEALTH_INIT_SCRIPT)
    return ctx


def is_challenge_page(*, title: str, html: str) -> bool:
    """Detect Akamai's bot-challenge interstitial.

    Both the title and the visible body shell contain "Challenge
    Validation" on every flavour of the challenge we've seen. Body
    HTML is short (~1.8 KB) compared to a real Practo page (>500 KB),
    but the size signal is wobbly so we go by content.
    """
    if title and CHALLENGE_TITLE in title:
        return True
    if CHALLENGE_TITLE in (html or ""):
        return True
    return False


def polite_sleep(low: float, high: float) -> None:
    """Sleep for a uniformly-random duration in `[low, high]` seconds.

    Used to break up uniform-timing automation fingerprints. Both
    Practo gateways add a short pause after navigation so the React
    app has time to hydrate `__REDUX_STATE__` before we read it.
    """
    if high <= low:
        time.sleep(max(0.0, low))
        return
    time.sleep(random.uniform(low, high))
