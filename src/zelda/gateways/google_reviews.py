"""Google Maps reviews gateway (Playwright-backed).

Drives a headless Chromium browser through the search-then-click flow
that mirrors a real user's path: navigate to maps.google.com, type the
clinic name into the search box, press Enter, click the Reviews tab,
sort by Newest, scroll the reviews panel until either `max_reviews`
items are loaded or the feed is exhausted, then extract each review.

Why search-then-click rather than a direct `/maps/place/?q=place_id:`
URL? Empirically, in 2026 Google Maps does not hydrate the Reviews
tab on the place sidebar when navigating to the redirect form — the
side panel renders only Overview and About. Going through the search
box triggers the full hydration path that real users see.

Rate-limit posture
------------------
Two layers, both configurable:

1. **Intra-place** (this gateway): random sleep between scroll actions
   (default 1.2–2.5 s), plus settle waits after navigation, tab clicks,
   and sort selection so we never look like an instant bot.
2. **Inter-place** (the controller): a longer random delay between
   processing different place_ids. Lives in the controller, not here.

A persistent Chromium instance is reused across `fetch_reviews` calls.
The caller (controller) is responsible for periodically resetting the
context (`reset_context()`) to flush accumulated cookies / fingerprint
signals — once every ~20 places is a sensible cadence.

Anti-block detection
--------------------
- URL containing `/sorry/` → Google's hard bot block page
- Presence of a recaptcha iframe
- "Our systems have detected unusual traffic" text on the page
- Reviews tab fails to appear within `page_load_timeout`

When detected, we return a `ReviewSet` with `fetch_status` set
("captcha" / "blocked" / "error") and whatever reviews we captured
before the block kicked in. We never retry through a block.
"""

import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from zelda.models.review import Review, ReviewSet


_MAPS_HOME = "https://www.google.com/maps?hl=en"

# Search input — Maps uses `input#searchboxinput` as the canonical id,
# but in some layouts it renders with a dynamic id like `ucc-1`. We
# prefer the canonical and fall back to "any role-combobox text input".
_SEARCH_INPUT_SELECTORS = [
    "input#searchboxinput",
    'input[name="q"]',
    'input[aria-label*="Search"]',
    'input[role="combobox"]',
    'input[type="text"]',
]

# CSS selector for review cards. `data-review-id` has been stable for
# years because it's how Maps internally addresses each review.
_REVIEW_ITEM_SELECTOR = "[data-review-id]"

# Sort dropdown trigger.
_SORT_BUTTON_SELECTOR = 'button[aria-label*="Sort reviews"]'

# `aria-label` text snippets we look for to confirm we're blocked.
_BLOCK_TEXT_MARKERS = [
    "unusual traffic",
    "Our systems have detected",
    "automated queries",
]

# Relative time conversion. Tuples of (regex, approximate_days_per_unit).
# Order matters — most specific first.
_RELATIVE_TIME_PATTERNS: list[tuple[re.Pattern[str], float]] = [
    (re.compile(r"^(\d+)\s+years?\s+ago", re.I), 365.0),
    (re.compile(r"^(\d+)\s+months?\s+ago", re.I), 30.0),
    (re.compile(r"^(\d+)\s+weeks?\s+ago", re.I), 7.0),
    (re.compile(r"^(\d+)\s+days?\s+ago", re.I), 1.0),
    (re.compile(r"^(\d+)\s+hours?\s+ago", re.I), 1.0 / 24),
    (re.compile(r"^(\d+)\s+minutes?\s+ago", re.I), 1.0 / 1440),
]
_RELATIVE_TIME_SINGULARS: dict[str, float] = {
    "a year ago": 365.0,
    "a month ago": 30.0,
    "a week ago": 7.0,
    "a day ago": 1.0,
    "yesterday": 1.0,
    "an hour ago": 1.0 / 24,
    "a minute ago": 1.0 / 1440,
    "moments ago": 0.0,
    "just now": 0.0,
    "today": 0.0,
}

# CONSENT cookie pre-set. Without this, a fresh Chromium hits Google's
# cookie consent wall and the Maps app never fully hydrates.
_CONSENT_COOKIES = [
    {
        "name": "CONSENT",
        "value": "YES+cb.20210720-07-p0.en+FX+410",
        "domain": ".google.com",
        "path": "/",
    },
    {
        "name": "SOCS",
        "value": "CAESHAgBEhJnd3NfMjAyMzA4MDgtMF9SQzIaAmVuIAEaBgiA_LyaBg",
        "domain": ".google.com",
        "path": "/",
    },
]

# Init script — minimal but the patches that actually matter for
# Google. webdriver, languages, plugins, chrome.runtime.
_STEALTH_INIT_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    window.chrome = { runtime: {} };
"""


# ── pure helpers (unit-testable, no I/O) ────────────────────────────


def parse_relative_time(
    text: str | None,
    *,
    anchor: datetime,
) -> datetime | None:
    """Convert a Google Maps relative time string into an approximate
    absolute datetime, anchored at `anchor` (typically the capture time).

    Returns None if the string can't be parsed. Treat the result as
    `±50%` of the implied granularity — Maps doesn't give exact
    timestamps in the public UI.
    """
    if not text:
        return None
    s = text.strip().lower()
    s = re.sub(r"^edited\s+", "", s)
    if s in _RELATIVE_TIME_SINGULARS:
        days = _RELATIVE_TIME_SINGULARS[s]
        return anchor - timedelta(days=days)
    for pattern, days_per_unit in _RELATIVE_TIME_PATTERNS:
        m = pattern.match(s)
        if m:
            count = int(m.group(1))
            return anchor - timedelta(days=count * days_per_unit)
    return None


def detect_block_signal(url: str, page_text: str) -> str | None:
    """Inspect the current URL and visible page text for signs Google
    is blocking us. Returns a short status string if blocked, else None.

    Possible return values: 'sorry_url' | 'captcha' | 'unusual_traffic'.
    """
    if "/sorry/" in url:
        return "sorry_url"
    lower = page_text.lower()
    for marker in _BLOCK_TEXT_MARKERS:
        if marker.lower() in lower:
            return "unusual_traffic"
    return None


def _polite_sleep(low: float, high: float) -> None:
    """Random sleep in the [low, high] interval. Used between scroll
    actions to avoid uniform-timing automation fingerprints."""
    if high <= low:
        time.sleep(low)
        return
    time.sleep(random.uniform(low, high))


# ── gateway ─────────────────────────────────────────────────────────


class GoogleReviewsGateway:
    """Playwright-backed scraper for Google Maps reviews.

    Use as a context manager:

        with GoogleReviewsGateway.launch() as gw:
            review_set = gw.fetch_reviews(
                place_id="ChIJ...",
                search_query="Sai Dental Clinic Ludhiana",
                max_reviews=500,
            )
    """

    def __init__(
        self,
        *,
        playwright: Playwright,
        browser: Browser,
        scroll_delay_range: tuple[float, float] = (1.2, 2.5),
        page_load_timeout_ms: int = 30_000,
        scroll_timeout_ms: int = 15_000,
        max_consecutive_no_progress: int = 3,
        max_seconds_per_place: float = 600.0,
        user_agent: str | None = None,
        viewport: tuple[int, int] = (1366, 900),
    ) -> None:
        self._playwright = playwright
        self._browser = browser
        self._scroll_delay_range = scroll_delay_range
        self._page_load_timeout_ms = page_load_timeout_ms
        self._scroll_timeout_ms = scroll_timeout_ms
        self._max_consecutive_no_progress = max_consecutive_no_progress
        self._max_seconds_per_place = max_seconds_per_place
        self._user_agent = user_agent or (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        )
        self._viewport = viewport
        self._context: BrowserContext | None = None

    @classmethod
    def launch(
        cls,
        *,
        headless: bool = True,
        use_real_chrome: bool = True,
        **kwargs: Any,
    ) -> "GoogleReviewsGateway":
        """Create a gateway with its own Playwright + Chromium instance.

        `use_real_chrome=True` (default) launches the system-installed
        Chrome via Playwright's `channel="chrome"`. Falls back to bundled
        Chromium if Chrome isn't found. Real Chrome has a different
        fingerprint than bundled Chromium and historically degrades less
        on Google properties.

        `headless=True` uses Chromium's "new" headless mode (closer to
        real Chrome rendering than the legacy mode).
        """
        pw = sync_playwright().start()
        launch_kwargs: dict[str, Any] = {
            "headless": headless,
            "args": ["--headless=new"] if headless else [],
        }
        if use_real_chrome:
            try:
                browser = pw.chromium.launch(channel="chrome", **launch_kwargs)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "channel=chrome failed; falling back to bundled chromium"
                )
                browser = pw.chromium.launch(**launch_kwargs)
        else:
            browser = pw.chromium.launch(**launch_kwargs)
        return cls(playwright=pw, browser=browser, **kwargs)

    # ── lifecycle ────────────────────────────────────────────────────

    def __enter__(self) -> "GoogleReviewsGateway":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:  # noqa: BLE001
                pass
            self._context = None
        try:
            self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._playwright.stop()
        except Exception:  # noqa: BLE001
            pass

    def reset_context(self) -> None:
        """Drop and recreate the BrowserContext to flush accumulated
        cookies / fingerprint signals. Caller (controller) decides
        cadence."""
        if self._context is not None:
            try:
                self._context.close()
            except Exception:  # noqa: BLE001
                pass
        self._context = self._browser.new_context(
            user_agent=self._user_agent,
            viewport={"width": self._viewport[0], "height": self._viewport[1]},
            locale="en-US",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        self._context.add_cookies(_CONSENT_COOKIES)
        self._context.add_init_script(_STEALTH_INIT_SCRIPT)

    def _ensure_context(self) -> BrowserContext:
        if self._context is None:
            self.reset_context()
        assert self._context is not None
        return self._context

    # ── public API ───────────────────────────────────────────────────

    def fetch_reviews(
        self,
        place_id: str,
        *,
        search_query: str,
        max_reviews: int = 1000,
        order: str = "newest_first",
        total_reviews_hint: int | None = None,
    ) -> ReviewSet:
        """Fetch up to `max_reviews` reviews for `place_id`.

        Required: `search_query` — a string that, when typed into Maps'
        search box, will land on this place's listing. Typically the
        clinic's name + city (e.g. "Sai Dental Clinic Ludhiana"). The
        controller constructs this from the lead's `name` + `city`.

        `total_reviews_hint` should be the `userRatingCount` from the
        Places API for this place; the resulting `ReviewSet` carries
        it as `total_reviews_per_gbp` so downstream consumers can
        detect truncation.

        Always returns a `ReviewSet`, even on failure. Caller checks
        `fetch_status` and `error_message` for non-OK runs.
        """
        if not place_id or not place_id.strip():
            raise ValueError("place_id must be non-empty")
        if not search_query or not search_query.strip():
            raise ValueError("search_query must be non-empty")
        if max_reviews < 1:
            raise ValueError("max_reviews must be >= 1")
        if order != "newest_first":
            raise ValueError(
                f"only order='newest_first' is supported, got {order!r}"
            )

        captured_at = datetime.now(timezone.utc)
        ctx = self._ensure_context()
        page = ctx.new_page()
        page.set_default_timeout(self._page_load_timeout_ms)

        reviews: list[Review] = []
        status: str = "ok"
        error_message: str | None = None

        try:
            self._navigate_via_search(page, search_query)

            block = detect_block_signal(page.url, page.content())
            if block:
                logger.error(
                    "reviews.blocked place_id={place_id} signal={signal}",
                    place_id=place_id, signal=block,
                )
                status = "captcha" if block == "captcha" else "blocked"
                error_message = f"block signal: {block}"
                page.close()
                return self._build_result(
                    place_id, reviews, total_reviews_hint, max_reviews,
                    order, captured_at, status, error_message,
                )

            self._open_reviews_tab(page)
            self._set_sort_to_newest(page)
            reviews = self._scroll_and_collect(page, place_id, max_reviews, captured_at)

            logger.info(
                "reviews.captured place_id={place_id} captured={n} "
                "max_reviews={cap}",
                place_id=place_id, n=len(reviews), cap=max_reviews,
            )
        except PlaywrightTimeoutError as e:
            status = "error"
            error_message = f"playwright timeout: {e}"
            logger.error(
                "reviews.timeout place_id={place_id} captured={n} error={err}",
                place_id=place_id, n=len(reviews), err=str(e),
            )
        except Exception as e:  # noqa: BLE001
            status = "error"
            error_message = f"unexpected: {type(e).__name__}: {e}"
            logger.error(
                "reviews.error place_id={place_id} captured={n} error={err}",
                place_id=place_id, n=len(reviews), err=error_message,
            )
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass

        if status == "error" and reviews:
            status = "partial"

        return self._build_result(
            place_id, reviews, total_reviews_hint, max_reviews,
            order, captured_at, status, error_message,
        )

    # ── internals ────────────────────────────────────────────────────

    def _navigate_via_search(self, page: Page, query: str) -> None:
        """Open Maps and type the query into the search box. This is
        the user-flow that triggers full sidebar hydration with the
        Reviews tab populated."""
        page.goto(_MAPS_HOME, wait_until="domcontentloaded")
        _polite_sleep(2.5, 4.0)

        # Try each candidate selector for the search input. Maps
        # sometimes renders with a dynamic id (e.g. `ucc-1`) instead of
        # `searchboxinput`.
        input_locator = None
        for sel in _SEARCH_INPUT_SELECTORS:
            cand = page.locator(sel).first
            try:
                cand.wait_for(timeout=3000)
                input_locator = cand
                break
            except PlaywrightTimeoutError:
                continue
        if input_locator is None:
            raise PlaywrightTimeoutError(
                "could not find Maps search input with any known selector"
            )
        input_locator.fill(query)
        _polite_sleep(0.5, 1.0)
        input_locator.press("Enter")
        # Place panel takes a few seconds to render fully
        _polite_sleep(5.0, 7.0)

    def _open_reviews_tab(self, page: Page) -> None:
        """Click the 'Reviews' tab on the place panel. ARIA-driven
        selector — has been stable because Maps must remain accessible."""
        try:
            page.get_by_role("tab", name="Reviews").click(
                timeout=self._scroll_timeout_ms
            )
            _polite_sleep(*self._scroll_delay_range)
        except PlaywrightTimeoutError as e:
            raise PlaywrightTimeoutError(
                f"could not find Reviews tab on the place panel: {e}"
            ) from e

    def _set_sort_to_newest(self, page: Page) -> None:
        """Click 'Sort reviews' → 'Newest'."""
        try:
            page.locator(_SORT_BUTTON_SELECTOR).first.click(
                timeout=self._scroll_timeout_ms
            )
            _polite_sleep(0.5, 0.9)
        except PlaywrightTimeoutError as e:
            raise PlaywrightTimeoutError(
                f"could not find Sort button: {e}"
            ) from e
        # Sort menu items are role=menuitemradio; "Newest" is one of them
        try:
            page.get_by_role("menuitemradio", name="Newest").click(
                timeout=self._scroll_timeout_ms
            )
            _polite_sleep(*self._scroll_delay_range)
        except PlaywrightTimeoutError:
            # Fallback: some Maps variants use role=menuitem
            page.get_by_role("menuitem", name="Newest").click(
                timeout=self._scroll_timeout_ms
            )
            _polite_sleep(*self._scroll_delay_range)

    def _scroll_and_collect(
        self,
        page: Page,
        place_id: str,
        max_reviews: int,
        captured_at: datetime,
    ) -> list[Review]:
        """Scroll the reviews scrollable ancestor until we have
        `max_reviews` *unique* items loaded or progress stalls. Then
        expand all 'More' buttons and extract every review.
        """
        deadline = time.monotonic() + self._max_seconds_per_place
        no_progress = 0
        last_unique = 0

        page.wait_for_selector(
            _REVIEW_ITEM_SELECTOR, timeout=self._scroll_timeout_ms
        )

        while True:
            if time.monotonic() > deadline:
                logger.warning(
                    "reviews.deadline place_id={place_id} unique_so_far={n}",
                    place_id=place_id, n=last_unique,
                )
                break

            unique_count = page.evaluate(
                "() => new Set(Array.from(document.querySelectorAll("
                "'[data-review-id]')).map(e => e.getAttribute('data-review-id'))"
                ").size"
            )
            if unique_count >= max_reviews:
                break

            if unique_count == last_unique:
                no_progress += 1
                if no_progress >= self._max_consecutive_no_progress:
                    break
            else:
                no_progress = 0
                last_unique = unique_count

            scrolled = page.evaluate(_SCROLL_FIRST_SCROLLABLE_ANCESTOR_JS)
            if not scrolled:
                logger.warning(
                    "reviews.no_scroll_target place_id={place_id} unique={n}",
                    place_id=place_id, n=last_unique,
                )
                break

            _polite_sleep(*self._scroll_delay_range)

        # Best-effort: expand any "More" buttons inside review cards so
        # we capture full text (Maps truncates long reviews otherwise).
        try:
            more_buttons = page.locator(
                'button:has-text("More"), button[aria-label="See more"]'
            )
            for i in range(min(more_buttons.count(), 1000)):
                try:
                    more_buttons.nth(i).click(timeout=400)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

        # Pull every (deduplicated) review's data via a single in-page
        # evaluate. Cheaper than crossing the JS↔Python bridge per row.
        raw_reviews = page.evaluate(_REVIEW_EXTRACTOR_JS)
        out: list[Review] = []
        for i, raw in enumerate(raw_reviews[:max_reviews], start=1):
            review = _build_review_from_raw(
                raw, place_id=place_id, sequence=i, anchor=captured_at,
            )
            if review is not None:
                out.append(review)
        return out

    def _build_result(
        self,
        place_id: str,
        reviews: list[Review],
        total_reviews_hint: int | None,
        max_reviews: int,
        order: str,
        captured_at: datetime,
        status: str,
        error_message: str | None,
    ) -> ReviewSet:
        earliest = min(
            (r.approx_publish_at for r in reviews if r.approx_publish_at),
            default=None,
        )
        latest = max(
            (r.approx_publish_at for r in reviews if r.approx_publish_at),
            default=None,
        )
        return ReviewSet(
            place_id=place_id,
            reviews=reviews,
            total_reviews_per_gbp=total_reviews_hint,
            capture_cap=max_reviews,
            capture_order=order,
            captured_at=captured_at,
            earliest_review_at=earliest,
            latest_review_at=latest,
            fetch_status=status,  # type: ignore[arg-type]
            error_message=error_message,
        )


# JS to scroll the first scrollable ancestor of a review item. We use
# this rather than a fixed selector because Maps' DOM rotates class
# names; the *structural* property "first ancestor with overflow-y:
# scroll/auto where scrollHeight > clientHeight" has been stable.
_SCROLL_FIRST_SCROLLABLE_ANCESTOR_JS = """
() => {
  const r = document.querySelector('[data-review-id]');
  if (!r) return false;
  let el = r;
  while (el && el !== document.body) {
    const cs = window.getComputedStyle(el);
    if (['scroll', 'auto'].includes(cs.overflowY) && el.scrollHeight > el.clientHeight) {
      el.scrollTo(0, el.scrollHeight);
      return true;
    }
    el = el.parentElement;
  }
  return false;
}
"""


# JS that runs in the page context. Returns one dict per *unique*
# review row (deduplicated by data-review-id; Maps virtualizes the
# feed and the same review may be in the DOM multiple times during
# scroll).
#
# Maps' DOM uses obfuscated class names that are stable across many
# months but rotate occasionally. The classes we depend on (verified
# via DOM probe):
#   .d4r55         — reviewer name text
#   .RfnDt         — reviewer's "X reviews" badge
#   .kvMYJc        — star rating wrapper (also has aria-label "N stars")
#   .rsqaWe        — relative time of the review itself
#   .MyEned        — wrapper around the review body text
#   .wiI7pd        — span containing the review body text (also used by
#                    owner response — distinguish via .CDe7pd ancestry)
#   .CDe7pd        — owner-response container (header + time + body)
#   .DZSIDd        — relative time of the owner response
# The row itself has `aria-label="<reviewer name>"` which is the
# cleanest source for the author name.
_REVIEW_EXTRACTOR_JS = r"""
() => {
  const seen = new Set();
  const rows = Array.from(document.querySelectorAll('[data-review-id]'))
    .filter(row => {
      const id = row.getAttribute('data-review-id') || '';
      if (!id || seen.has(id)) return false;
      seen.add(id);
      return true;
    });

  return rows.map(row => {
    const reviewId = row.getAttribute('data-review-id') || '';

    // Author name: row's aria-label is the cleanest source.
    // Fallback: .d4r55 (the reviewer name button text).
    let authorName = (row.getAttribute('aria-label') || '').trim();
    if (!authorName) {
      const nameEl = row.querySelector('.d4r55');
      if (nameEl) authorName = (nameEl.textContent || '').trim();
    }

    // Author URL — the button wrapping the name links to the user's
    // reviewer profile (sometimes anchor, sometimes button with data-href).
    const authorAnchor = row.querySelector('a[href*="/maps/contrib/"], button[data-href]');
    let authorUrl = '';
    if (authorAnchor) {
      authorUrl = authorAnchor.getAttribute('href')
                || authorAnchor.getAttribute('data-href') || '';
    }

    // Author photo — first <img> in the row is the avatar.
    const authorImg = row.querySelector('img');
    const authorPhotoUrl = authorImg ? authorImg.getAttribute('src') : '';

    // Star rating: aria-label like "5 stars" on the .kvMYJc span.
    let rating = null;
    const ratingEl = row.querySelector(
      '.kvMYJc[aria-label*="star"], [role="img"][aria-label*="star"]'
    );
    if (ratingEl) {
      const m = (ratingEl.getAttribute('aria-label') || '').match(/(\d+)\s*star/i);
      if (m) rating = parseInt(m[1], 10);
    }

    // Relative publish time — .rsqaWe is unambiguous (NOT the owner
    // response time, which lives under .CDe7pd with its own class).
    const timeEl = row.querySelector('.rsqaWe');
    const relativeTime = timeEl ? (timeEl.textContent || '').trim() : '';

    // Review body — .MyEned > .wiI7pd. NOT the .wiI7pd inside
    // .CDe7pd, which is the owner response body.
    let text = '';
    const reviewBodyEl = row.querySelector('.MyEned .wiI7pd');
    if (reviewBodyEl) {
      text = (reviewBodyEl.textContent || '').trim();
    }
    if (!text) {
      // Fallback: any .wiI7pd not inside an owner-response container.
      for (const el of row.querySelectorAll('.wiI7pd')) {
        if (!el.closest('.CDe7pd')) {
          text = (el.textContent || '').trim();
          break;
        }
      }
    }

    // Owner response — fully contained in .CDe7pd
    let ownerResponseText = '';
    let ownerResponseRelative = '';
    const ownerContainer = row.querySelector('.CDe7pd');
    if (ownerContainer) {
      const ownerTimeEl = ownerContainer.querySelector('.DZSIDd');
      if (ownerTimeEl) ownerResponseRelative = (ownerTimeEl.textContent || '').trim();
      const ownerBodyEl = ownerContainer.querySelector('.wiI7pd');
      if (ownerBodyEl) ownerResponseText = (ownerBodyEl.textContent || '').trim();
    }

    // Reviewer's review-count badge (e.g. "1 review", "12 reviews").
    let reviewerReviewCount = null;
    const countEl = row.querySelector('.RfnDt');
    if (countEl) {
      const m = (countEl.textContent || '').match(/(\d+)/);
      if (m) reviewerReviewCount = parseInt(m[1], 10);
    }

    // Likes count — the Like button shows a number when others have
    // marked the review helpful.
    let likes = null;
    const likeBtn = row.querySelector('button[aria-label="Like"]');
    if (likeBtn) {
      const m = (likeBtn.textContent || '').match(/(\d+)/);
      if (m) likes = parseInt(m[1], 10);
    }

    // Photo thumbnails attached to the review.
    const photoUrls = [];
    row.querySelectorAll('button[style*="background-image"]').forEach(b => {
      const m = (b.getAttribute('style') || '').match(/url\("?([^")]+)"?\)/);
      if (m) photoUrls.push(m[1]);
    });

    return {
      review_id: reviewId,
      rating,
      text,
      author_name: authorName,
      author_url: authorUrl,
      author_photo_url: authorPhotoUrl,
      reviewer_review_count: reviewerReviewCount,
      relative_publish_time: relativeTime,
      owner_response_text: ownerResponseText || null,
      owner_response_relative_time: ownerResponseRelative || null,
      photo_urls: photoUrls,
      likes_count: likes,
    };
  });
}
"""


def _build_review_from_raw(
    raw: dict[str, Any],
    *,
    place_id: str,
    sequence: int,
    anchor: datetime,
) -> Review | None:
    """Convert a JS-extracted dict into a `Review`. Returns None if the
    dict is too sparse to be useful (no review_id and no text)."""
    review_id = (raw.get("review_id") or "").strip()
    text = (raw.get("text") or "").strip() or None
    if not review_id and not text:
        return None
    if not review_id:
        review_id = f"unknown-{place_id}-{sequence}"

    relative_time = raw.get("relative_publish_time") or None
    approx_publish = parse_relative_time(relative_time, anchor=anchor)
    owner_relative = raw.get("owner_response_relative_time") or None
    owner_approx = parse_relative_time(owner_relative, anchor=anchor)

    photo_urls = raw.get("photo_urls") or []
    if not isinstance(photo_urls, list):
        photo_urls = []

    return Review(
        review_id=review_id,
        place_id=place_id,
        rating=raw.get("rating"),
        text=text,
        author_name=(raw.get("author_name") or None) or None,
        author_url=(raw.get("author_url") or None) or None,
        author_photo_url=(raw.get("author_photo_url") or None) or None,
        relative_publish_time=relative_time,
        approx_publish_at=approx_publish,
        owner_response_text=raw.get("owner_response_text") or None,
        owner_response_relative_time=owner_relative,
        owner_response_approx_at=owner_approx,
        photo_urls=[p for p in photo_urls if isinstance(p, str)],
        likes_count=raw.get("likes_count"),
        sequence_in_capture=sequence,
        raw_json=raw,
    )
