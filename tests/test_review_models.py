from datetime import datetime, timedelta, timezone

import pytest

from zelda.models.review import (
    Review,
    ReviewSet,
    ReviewSetTruncated,
    ReviewSetWindowNotCovered,
)


# ── helpers ─────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)


def _mk_review(review_id: str = "r1", rating: int = 5, **overrides) -> Review:
    base = dict(
        review_id=review_id,
        place_id="ChIJ_X",
        rating=rating,
        text=f"review {review_id}",
        author_name=f"user_{review_id}",
        relative_publish_time="a month ago",
        approx_publish_at=_now() - timedelta(days=30),
    )
    base.update(overrides)
    return Review(**base)


def _mk_reviewset(
    *,
    n_reviews: int = 5,
    total_per_gbp: int | None = 5,
    cap: int = 1000,
    order: str = "newest_first",
    earliest: datetime | None = None,
    latest: datetime | None = None,
    fetch_status: str = "ok",
) -> ReviewSet:
    reviews = [
        _mk_review(
            review_id=f"r{i}",
            approx_publish_at=_now() - timedelta(days=i),
            sequence_in_capture=i + 1,
        )
        for i in range(n_reviews)
    ]
    return ReviewSet(
        place_id="ChIJ_X",
        reviews=reviews,
        total_reviews_per_gbp=total_per_gbp,
        capture_cap=cap,
        capture_order=order,
        captured_at=_now(),
        earliest_review_at=earliest or (_now() - timedelta(days=n_reviews)),
        latest_review_at=latest or _now(),
        fetch_status=fetch_status,
    )


# ── Review ──────────────────────────────────────────────────────────────


def test_review_round_trips_through_json():
    r = _mk_review("r1", rating=4)
    dumped = r.model_dump(mode="json")
    restored = Review.model_validate(dumped)
    assert restored == r


def test_review_handles_missing_optionals():
    r = Review(review_id="r1", place_id="ChIJ_X")
    assert r.rating is None
    assert r.text is None
    assert r.photo_urls == []
    assert r.raw_json == {}


def test_review_preserves_raw_json_for_unknown_fields():
    r = Review(
        review_id="r1",
        place_id="ChIJ_X",
        raw_json={"weird_future_field": "value"},
    )
    assert r.raw_json["weird_future_field"] == "value"


# ── ReviewSet derived properties ────────────────────────────────────────


def test_reviewset_reviews_captured_counts_list():
    rs = _mk_reviewset(n_reviews=3, total_per_gbp=3)
    assert rs.reviews_captured == 3


def test_reviewset_is_truncated_false_when_complete():
    rs = _mk_reviewset(n_reviews=5, total_per_gbp=5)
    assert rs.is_truncated is False


def test_reviewset_is_truncated_true_when_partial():
    rs = _mk_reviewset(n_reviews=5, total_per_gbp=100, cap=5)
    assert rs.is_truncated is True


def test_reviewset_is_truncated_false_when_total_unknown():
    """Conservative: unknown total → False, so stat functions don't
    block. The strict check is `assert_complete()`."""
    rs = _mk_reviewset(n_reviews=5, total_per_gbp=None)
    assert rs.is_truncated is False


# ── assert_complete contract ───────────────────────────────────────────


def test_assert_complete_passes_when_truly_complete():
    rs = _mk_reviewset(n_reviews=5, total_per_gbp=5)
    rs.assert_complete()  # must not raise


def test_assert_complete_raises_when_truncated():
    rs = _mk_reviewset(n_reviews=10, total_per_gbp=100, cap=10)
    with pytest.raises(ReviewSetTruncated, match="truncated"):
        rs.assert_complete()


def test_assert_complete_raises_when_total_unknown():
    """`assert_complete` is the strict path: unknown total = unverifiable
    = raises. Callers that don't need full universe should not call it."""
    rs = _mk_reviewset(n_reviews=5, total_per_gbp=None)
    with pytest.raises(ReviewSetTruncated, match="unverifiable"):
        rs.assert_complete()


# ── qualified_text contract ────────────────────────────────────────────


def test_qualified_text_complete_set_includes_count_and_order():
    rs = _mk_reviewset(n_reviews=5, total_per_gbp=5)
    out = rs.qualified_text("12% mention 'didn't pick up'")
    assert "5 reviews captured" in out
    assert "newest_first" in out
    assert "12% mention" in out


def test_qualified_text_truncated_set_includes_truncation_marker():
    rs = _mk_reviewset(n_reviews=1000, total_per_gbp=2500, cap=1000)
    out = rs.qualified_text("12% mention 'didn't pick up'")
    assert "1000 of 2500 reviews" in out
    assert "truncated" in out
    assert "cap=1000" in out


def test_qualified_text_includes_date_range_when_known():
    rs = _mk_reviewset(
        n_reviews=5,
        total_per_gbp=5,
        earliest=datetime(2025, 1, 1, tzinfo=timezone.utc),
        latest=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    out = rs.qualified_text(42)
    assert "2025-01-01" in out
    assert "2026-04-01" in out


# ── assert_window_covered contract ─────────────────────────────────────


def test_assert_window_covered_passes_when_window_fully_covered():
    """We have reviews going back 100 days; asking about last 30 days. OK."""
    rs = _mk_reviewset(
        n_reviews=10,
        earliest=_now() - timedelta(days=100),
        latest=_now(),
    )
    rs.assert_window_covered(timedelta(days=30))  # must not raise


def test_assert_window_covered_raises_when_oldest_review_too_recent():
    """We have reviews going back only 30 days; asking about last 90 days.
    Truncation cut off the older end — any rate computation would be wrong."""
    rs = _mk_reviewset(
        n_reviews=10,
        total_per_gbp=500,
        cap=10,
        earliest=_now() - timedelta(days=30),
        latest=_now(),
    )
    with pytest.raises(ReviewSetWindowNotCovered, match="window"):
        rs.assert_window_covered(timedelta(days=90))


def test_assert_window_covered_raises_when_earliest_unknown():
    rs = _mk_reviewset(n_reviews=5, total_per_gbp=5)
    rs.earliest_review_at = None  # type: ignore
    with pytest.raises(ReviewSetWindowNotCovered, match="unknown"):
        rs.assert_window_covered(timedelta(days=30))


# ── empty set semantics ────────────────────────────────────────────────


def test_empty_reviewset_when_capture_blocked_or_no_reviews():
    """A blocked or zero-review capture should still construct cleanly
    with status flags."""
    rs = ReviewSet(
        place_id="ChIJ_X",
        reviews=[],
        total_reviews_per_gbp=0,
        capture_cap=1000,
        capture_order="newest_first",
        captured_at=_now(),
        fetch_status="ok",
    )
    assert rs.reviews_captured == 0
    assert rs.is_truncated is False  # 0 captured of 0 total = complete


def test_blocked_set_carries_status_and_message():
    rs = ReviewSet(
        place_id="ChIJ_X",
        reviews=[],
        total_reviews_per_gbp=300,
        capture_cap=1000,
        capture_order="newest_first",
        captured_at=_now(),
        fetch_status="captcha",
        error_message="hit recaptcha after scroll #4",
    )
    assert rs.fetch_status == "captcha"
    assert "recaptcha" in (rs.error_message or "")
    assert rs.is_truncated is True  # 0 < 300


# ── round-trip through JSON ────────────────────────────────────────────


def test_reviewset_round_trips_through_json():
    rs = _mk_reviewset(n_reviews=3, total_per_gbp=10, cap=1000)
    dumped = rs.model_dump(mode="json")
    restored = ReviewSet.model_validate(dumped)
    assert restored.place_id == rs.place_id
    assert len(restored.reviews) == 3
    assert restored.capture_cap == 1000
    assert restored.is_truncated is True
