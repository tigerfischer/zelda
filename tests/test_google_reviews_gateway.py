"""Tests for the Playwright-backed reviews gateway.

The Playwright orchestration itself is smoke-tested live (see
scripts/smoke_reviews.py); this file only unit-tests the *pure*
helpers — relative time parsing, block detection, raw → Review
conversion — so the brittle browser parts are isolated from the
deterministic parsing parts.
"""

from datetime import datetime, timedelta, timezone

import pytest

from zelda.gateways.google_reviews import (
    _build_review_from_raw,
    detect_block_signal,
    parse_relative_time,
)


# ── parse_relative_time ─────────────────────────────────────────────────


_ANCHOR = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_relative_time_returns_none_for_blank_input():
    assert parse_relative_time(None, anchor=_ANCHOR) is None
    assert parse_relative_time("", anchor=_ANCHOR) is None
    assert parse_relative_time("   ", anchor=_ANCHOR) is None


def test_parse_relative_time_returns_none_for_unrecognized():
    assert parse_relative_time("recently", anchor=_ANCHOR) is None
    assert parse_relative_time("very long ago", anchor=_ANCHOR) is None


@pytest.mark.parametrize(
    "phrase,expected_days_back",
    [
        ("today", 0),
        ("just now", 0),
        ("moments ago", 0),
        ("yesterday", 1),
        ("a day ago", 1),
        ("a week ago", 7),
        ("a month ago", 30),
        ("a year ago", 365),
        ("an hour ago", 1 / 24),
        ("a minute ago", 1 / 1440),
    ],
)
def test_parse_relative_time_singulars(phrase, expected_days_back):
    out = parse_relative_time(phrase, anchor=_ANCHOR)
    assert out is not None
    assert out == _ANCHOR - timedelta(days=expected_days_back)


@pytest.mark.parametrize(
    "phrase,expected_days_back",
    [
        ("2 days ago", 2),
        ("3 weeks ago", 21),
        ("6 months ago", 180),
        ("2 years ago", 730),
        ("11 hours ago", 11 / 24),
    ],
)
def test_parse_relative_time_plurals(phrase, expected_days_back):
    out = parse_relative_time(phrase, anchor=_ANCHOR)
    assert out is not None
    assert out == _ANCHOR - timedelta(days=expected_days_back)


def test_parse_relative_time_strips_edited_prefix():
    """Maps sometimes shows 'Edited 2 weeks ago' for edited reviews."""
    out = parse_relative_time("Edited 2 weeks ago", anchor=_ANCHOR)
    assert out == _ANCHOR - timedelta(days=14)


def test_parse_relative_time_case_insensitive():
    a = parse_relative_time("A WEEK AGO", anchor=_ANCHOR)
    b = parse_relative_time("a week ago", anchor=_ANCHOR)
    assert a == b == _ANCHOR - timedelta(days=7)


# ── detect_block_signal ─────────────────────────────────────────────────


def test_detect_block_signal_clean_page_returns_none():
    assert detect_block_signal(
        url="https://www.google.com/maps/place/?q=place_id:ChIJ_X",
        page_text="Reviews\n5 stars\nGreat service",
    ) is None


def test_detect_block_signal_sorry_url():
    out = detect_block_signal(
        url="https://www.google.com/sorry/?continue=...",
        page_text="something",
    )
    assert out == "sorry_url"


def test_detect_block_signal_unusual_traffic_text():
    out = detect_block_signal(
        url="https://www.google.com/maps/place/?q=...",
        page_text="Our systems have detected unusual traffic from your computer network.",
    )
    assert out == "unusual_traffic"


def test_detect_block_signal_automated_queries_text():
    out = detect_block_signal(
        url="https://www.google.com/maps/place/?q=...",
        page_text="To continue, please type the characters... automated queries...",
    )
    assert out == "unusual_traffic"


def test_detect_block_signal_case_insensitive_text():
    out = detect_block_signal(
        url="https://www.google.com/maps/place/?q=...",
        page_text="UNUSUAL TRAFFIC detected",
    )
    assert out == "unusual_traffic"


# ── _build_review_from_raw ──────────────────────────────────────────────


def _raw_review(**overrides):
    base = {
        "review_id": "ChdDSUhNMG9nS0VJQ0FnSUR3",
        "rating": 5,
        "text": "Great clinic, very professional staff.",
        "author_name": "Some Reviewer",
        "author_url": "https://www.google.com/maps/contrib/123",
        "author_photo_url": "https://lh3.googleusercontent.com/a/abc",
        "relative_publish_time": "a month ago",
        "owner_response_text": None,
        "owner_response_relative_time": None,
        "photo_urls": [],
        "likes_count": None,
    }
    base.update(overrides)
    return base


def test_build_review_from_raw_happy_path():
    raw = _raw_review()
    out = _build_review_from_raw(raw, place_id="ChIJ_X", sequence=1, anchor=_ANCHOR)
    assert out is not None
    assert out.review_id == raw["review_id"]
    assert out.place_id == "ChIJ_X"
    assert out.rating == 5
    assert out.text == "Great clinic, very professional staff."
    assert out.author_name == "Some Reviewer"
    assert out.relative_publish_time == "a month ago"
    assert out.approx_publish_at == _ANCHOR - timedelta(days=30)
    assert out.sequence_in_capture == 1
    assert out.raw_json == raw  # full lossless preservation


def test_build_review_from_raw_with_owner_response():
    raw = _raw_review(
        owner_response_text="Thank you for your kind words!",
        owner_response_relative_time="3 weeks ago",
    )
    out = _build_review_from_raw(raw, place_id="ChIJ_X", sequence=1, anchor=_ANCHOR)
    assert out is not None
    assert out.owner_response_text == "Thank you for your kind words!"
    assert out.owner_response_relative_time == "3 weeks ago"
    assert out.owner_response_approx_at == _ANCHOR - timedelta(days=21)


def test_build_review_from_raw_with_photos_and_likes():
    raw = _raw_review(
        photo_urls=["https://lh3/photo1", "https://lh3/photo2"],
        likes_count=3,
    )
    out = _build_review_from_raw(raw, place_id="ChIJ_X", sequence=1, anchor=_ANCHOR)
    assert out is not None
    assert out.photo_urls == ["https://lh3/photo1", "https://lh3/photo2"]
    assert out.likes_count == 3


def test_build_review_from_raw_filters_non_string_photo_urls():
    raw = _raw_review(photo_urls=["https://lh3/p1", None, 42, "https://lh3/p2"])
    out = _build_review_from_raw(raw, place_id="ChIJ_X", sequence=1, anchor=_ANCHOR)
    assert out is not None
    assert out.photo_urls == ["https://lh3/p1", "https://lh3/p2"]


def test_build_review_from_raw_synthesizes_id_when_missing():
    """If the DOM didn't expose a data-review-id but did give us text,
    we still want to keep the review — synthesize a placeholder ID
    from sequence so it's stable within the capture."""
    raw = _raw_review(review_id="")
    out = _build_review_from_raw(raw, place_id="ChIJ_X", sequence=42, anchor=_ANCHOR)
    assert out is not None
    assert out.review_id == "unknown-ChIJ_X-42"


def test_build_review_from_raw_returns_none_for_empty_review():
    """A row with no review_id AND no text is too sparse to keep."""
    raw = _raw_review(review_id="", text="")
    out = _build_review_from_raw(raw, place_id="ChIJ_X", sequence=1, anchor=_ANCHOR)
    assert out is None


def test_build_review_from_raw_preserves_unknown_relative_time_as_none():
    raw = _raw_review(relative_publish_time="some weird format")
    out = _build_review_from_raw(raw, place_id="ChIJ_X", sequence=1, anchor=_ANCHOR)
    assert out is not None
    assert out.relative_publish_time == "some weird format"
    assert out.approx_publish_at is None


def test_build_review_from_raw_handles_corrupt_photo_urls_field():
    """If JS gave us something weird (not a list), we should not crash."""
    raw = _raw_review()
    raw["photo_urls"] = "not a list"
    out = _build_review_from_raw(raw, place_id="ChIJ_X", sequence=1, anchor=_ANCHOR)
    assert out is not None
    assert out.photo_urls == []
