"""Review models.

`Review` is one Google Maps review for one place.

`ReviewSet` is the value object that bundles a list of reviews with the
metadata about *how* they were captured. Downstream code consumes
`ReviewSet`, never bare `list[Review]`, so the truncation flag, total
count, and capture order are physically inseparable from the data —
making it impossible to write a stat function that silently lies about
"% of reviews mentioning X" when the captured set is a biased subset.

The three contract patterns enforced by `ReviewSet` (see methods at the
bottom of the class):

1. `assert_complete()` — for stats that quote the *full review universe*.
   Raises if the captured set is truncated; forces the caller to either
   widen the cap or rephrase.
2. `qualified_text(...)` — wraps a numeric result with a human-readable
   bound caveat ("of the 1000 newest reviews captured between {date1}
   and {date2}, ..."). Bare numbers should never escape stat functions.
3. `assert_window_covered(window)` — for time-bound velocity / trend
   functions. Raises if the captured set's earliest review is *newer*
   than the requested window start (which means we don't have data for
   the older part of the window and any rate computation would be wrong).
"""

from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


CaptureOrder = Literal["newest_first", "oldest_first", "all"]
"""How the gateway sequenced the reviews when it captured them.

- `newest_first` — index 0 of `ReviewSet.reviews` is the newest review;
  if truncated, we have the newest N and missed the older tail.
- `oldest_first` — opposite. If truncated, we have the oldest N and
  missed the newer tail.
- `all` — captured everything (`is_truncated == False`); ordering is
  whatever Google returned.
"""


class Review(BaseModel):
    """One Google Maps review.

    `relative_publish_time` is what Google's UI shows ("a month ago",
    "in the last week"). `approx_publish_at` is our best-effort
    conversion to absolute time, anchored against the capture moment;
    granularity is whatever the relative phrase implies (week/month/year).
    Treat `approx_publish_at` as ±50% of that granularity.
    """

    model_config = ConfigDict(extra="ignore")

    review_id: str
    place_id: str

    rating: int | None = None
    text: str | None = None
    language: str | None = None

    author_name: str | None = None
    author_url: str | None = None
    author_photo_url: str | None = None

    relative_publish_time: str | None = None
    approx_publish_at: datetime | None = None

    owner_response_text: str | None = None
    owner_response_relative_time: str | None = None
    owner_response_approx_at: datetime | None = None

    photo_urls: list[str] = Field(default_factory=list)
    likes_count: int | None = None

    sequence_in_capture: int | None = None
    raw_json: dict[str, Any] = Field(default_factory=dict)


class ReviewSetTruncated(Exception):
    """Raised when a stat function requires the full review universe but
    the captured set is truncated."""


class ReviewSetWindowNotCovered(Exception):
    """Raised when a time-bound stat asks for a window the captured set
    doesn't cover (typically because truncation cut off the older end)."""


class ReviewSet(BaseModel):
    """A set of reviews captured for one place, plus the metadata about
    how that capture was done. The metadata is the contract that lets
    downstream code know whether it can answer the question being asked.
    """

    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    place_id: str
    reviews: list[Review] = Field(default_factory=list)

    total_reviews_per_gbp: int | None = None
    """`userRatingCount` from the Places API at the time of capture.
    None means we don't know (the gateway didn't pass it in)."""

    capture_cap: int
    """The configured ceiling on reviews to capture this run."""

    capture_order: CaptureOrder
    """How the captured reviews were sequenced. Drives interpretation
    when `is_truncated` is True."""

    captured_at: datetime
    """When the capture run finished."""

    earliest_review_at: datetime | None = None
    latest_review_at: datetime | None = None
    """Approximate timestamps of the oldest and newest captured reviews
    (using each review's `approx_publish_at`). Used by
    `assert_window_covered` to detect truncation-induced gaps."""

    fetch_status: Literal["ok", "captcha", "blocked", "partial", "error"] = "ok"
    """How the capture itself went. `ok` = clean run. `partial` = some
    reviews fetched then aborted (e.g. CAPTCHA mid-scroll). `captcha`
    or `blocked` = stopped before any reviews. `error` = unexpected."""

    error_message: str | None = None

    # ── derived properties ──────────────────────────────────────────

    @property
    def reviews_captured(self) -> int:
        return len(self.reviews)

    @property
    def is_truncated(self) -> bool:
        """True iff we know we missed reviews. False if we captured
        everything OR if `total_reviews_per_gbp` is unknown (we conserve-
        atively return False rather than True in the unknown case to
        avoid blocking stat functions that don't actually need it).
        Callers that want strict assertion should use `assert_complete`
        which inspects the underlying state explicitly."""
        if self.total_reviews_per_gbp is None:
            return False
        return self.reviews_captured < self.total_reviews_per_gbp

    # ── three downstream contracts ──────────────────────────────────

    def assert_complete(self) -> None:
        """Use before computing a stat over the *full review universe*
        ("X% of all reviews mention Y"). Raises if we know the set is
        incomplete or if completeness is unverifiable."""
        if self.total_reviews_per_gbp is None:
            raise ReviewSetTruncated(
                f"completeness unverifiable for place_id={self.place_id}: "
                "total_reviews_per_gbp is None"
            )
        if self.is_truncated:
            raise ReviewSetTruncated(
                f"captured set is truncated for place_id={self.place_id}: "
                f"{self.reviews_captured} / {self.total_reviews_per_gbp} reviews "
                f"(capture_cap={self.capture_cap}, order={self.capture_order})"
            )

    def qualified_text(self, value: object) -> str:
        """Render a stat result with the captured-set bounds prepended
        so a human reading it can never mistake it for a full-universe
        number."""
        bounds = (
            f"{self.reviews_captured} reviews captured "
            f"(order={self.capture_order})"
        )
        if self.is_truncated:
            bounds = (
                f"{self.reviews_captured} of {self.total_reviews_per_gbp} reviews "
                f"({self.capture_order}; truncated at cap={self.capture_cap})"
            )
        if self.earliest_review_at and self.latest_review_at:
            bounds += (
                f", spanning {self.earliest_review_at.date()} → "
                f"{self.latest_review_at.date()}"
            )
        return f"of the {bounds}: {value}"

    def assert_window_covered(self, window: timedelta) -> None:
        """For time-bound velocity / trend stats. Raises if the captured
        set's earliest review is more recent than `now - window` (which
        means truncation cut off the older end of the window we're being
        asked about)."""
        if self.earliest_review_at is None:
            raise ReviewSetWindowNotCovered(
                f"cannot verify window coverage for place_id={self.place_id}: "
                "earliest_review_at unknown"
            )
        cutoff = self.captured_at - window
        if self.earliest_review_at > cutoff:
            raise ReviewSetWindowNotCovered(
                f"captured set does not cover window for place_id={self.place_id}: "
                f"earliest captured review is {self.earliest_review_at.date()}, "
                f"window asks back to {cutoff.date()}"
            )
