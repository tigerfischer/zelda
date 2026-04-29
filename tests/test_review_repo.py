from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from zelda.models.review import Review, ReviewSet
from zelda.repositories.review_repo import ReviewRepository


_T1 = datetime(2026, 4, 29, 10, 0, 0, tzinfo=timezone.utc)
_T2 = _T1 + timedelta(hours=1)
_T3 = _T1 + timedelta(hours=2)


# ── helpers + fixtures ──────────────────────────────────────────────────


def _mk_review(
    review_id: str = "r1",
    place_id: str = "ChIJ_X",
    rating: int = 5,
    text: str = "Great clinic",
    publish_offset_days: int = 30,
    owner_response: str | None = None,
    **overrides,
) -> Review:
    base = dict(
        review_id=review_id,
        place_id=place_id,
        rating=rating,
        text=text,
        author_name=f"user_{review_id}",
        relative_publish_time=f"{publish_offset_days // 30 or 1} months ago",
        approx_publish_at=_T1 - timedelta(days=publish_offset_days),
        sequence_in_capture=1,
        raw_json={"some": "raw", "review_id": review_id},
    )
    if owner_response:
        base["owner_response_text"] = owner_response
        base["owner_response_relative_time"] = "1 month ago"
        base["owner_response_approx_at"] = _T1 - timedelta(days=publish_offset_days - 1)
    base.update(overrides)
    return Review(**base)


def _mk_reviewset(
    *,
    place_id: str = "ChIJ_X",
    reviews: list[Review] | None = None,
    total_per_gbp: int | None = None,
    cap: int = 1000,
    order: str = "newest_first",
    captured_at: datetime = _T1,
    fetch_status: str = "ok",
) -> ReviewSet:
    reviews = reviews if reviews is not None else [_mk_review()]
    return ReviewSet(
        place_id=place_id,
        reviews=reviews,
        total_reviews_per_gbp=total_per_gbp,
        capture_cap=cap,
        capture_order=order,
        captured_at=captured_at,
        earliest_review_at=min(
            (r.approx_publish_at for r in reviews if r.approx_publish_at),
            default=None,
        ),
        latest_review_at=max(
            (r.approx_publish_at for r in reviews if r.approx_publish_at),
            default=None,
        ),
        fetch_status=fetch_status,
    )


@pytest.fixture
def repo():
    r = ReviewRepository(":memory:")
    yield r
    r.close()


# ── schema / lifecycle ──────────────────────────────────────────────────


def test_init_schema_is_idempotent():
    r = ReviewRepository(":memory:")
    r.init_schema()
    r.init_schema()
    r.close()


def test_repo_creates_parent_dir(tmp_path: Path):
    db = tmp_path / "deep" / "subdir" / "reviews.db"
    r = ReviewRepository(db)
    try:
        assert db.parent.exists()
    finally:
        r.close()


def test_repo_is_context_manager(tmp_path: Path):
    db = tmp_path / "x.db"
    with ReviewRepository(db) as r:
        r.save_capture(_mk_reviewset(), capture_id="cap-1")
        assert r.count_reviews_for_place("ChIJ_X") == 1


# ── save_capture ────────────────────────────────────────────────────────


def test_save_capture_writes_one_capture_row(repo: ReviewRepository):
    repo.save_capture(_mk_reviewset(), capture_id="cap-1")
    assert repo.count_captures_for_place("ChIJ_X") == 1


def test_save_capture_writes_review_rows(repo: ReviewRepository):
    rs = _mk_reviewset(reviews=[_mk_review("r1"), _mk_review("r2")])
    repo.save_capture(rs, capture_id="cap-1")
    assert repo.count_reviews_for_place("ChIJ_X") == 2


def test_save_capture_with_zero_reviews_writes_only_capture_row(repo: ReviewRepository):
    rs = _mk_reviewset(reviews=[])
    repo.save_capture(rs, capture_id="cap-empty")
    assert repo.count_captures_for_place("ChIJ_X") == 1
    assert repo.count_reviews_for_place("ChIJ_X") == 0


def test_save_capture_rejects_blank_capture_id(repo: ReviewRepository):
    with pytest.raises(ValueError, match="capture_id"):
        repo.save_capture(_mk_reviewset(), capture_id="")
    with pytest.raises(ValueError, match="capture_id"):
        repo.save_capture(_mk_reviewset(), capture_id="   ")


# ── upsert: re-capture preserves first sighting ────────────────────────


def test_re_capture_preserves_first_seen_and_discovered_at(repo: ReviewRepository):
    """Critical: when we re-capture later, `first_seen_capture_id` and
    `discovered_at` must NOT change — they record the original sighting."""
    rs1 = _mk_reviewset(captured_at=_T1)
    repo.save_capture(rs1, capture_id="cap-1")

    cur = repo._conn.execute(  # noqa: SLF001
        "SELECT first_seen_capture_id, discovered_at, last_seen_capture_id, "
        "last_seen_at FROM reviews WHERE review_id = 'r1'"
    )
    row1 = cur.fetchone()

    rs2 = _mk_reviewset(captured_at=_T3, reviews=[_mk_review("r1", text="UPDATED")])
    repo.save_capture(rs2, capture_id="cap-2")

    cur = repo._conn.execute(  # noqa: SLF001
        "SELECT first_seen_capture_id, discovered_at, last_seen_capture_id, "
        "last_seen_at, text FROM reviews WHERE review_id = 'r1'"
    )
    row2 = cur.fetchone()

    assert row2["first_seen_capture_id"] == "cap-1"
    assert row2["discovered_at"] == row1["discovered_at"]
    assert row2["last_seen_capture_id"] == "cap-2"
    assert row2["last_seen_at"] != row1["last_seen_at"]
    assert row2["text"] == "UPDATED"


def test_re_capture_overwrites_mutable_fields(repo: ReviewRepository):
    """Owner can edit their response. Subsequent captures should reflect
    updates to mutable fields like text, owner_response_text, likes_count."""
    rs1 = _mk_reviewset(reviews=[_mk_review("r1", text="Original", owner_response="Hi")])
    repo.save_capture(rs1, capture_id="cap-1")

    rs2 = _mk_reviewset(
        captured_at=_T2,
        reviews=[_mk_review("r1", text="Edited", owner_response="Updated thanks")],
    )
    repo.save_capture(rs2, capture_id="cap-2")

    leads = repo.get_reviews_for_place("ChIJ_X")
    assert len(leads) == 1
    assert leads[0].text == "Edited"
    assert leads[0].owner_response_text == "Updated thanks"


# ── reads ──────────────────────────────────────────────────────────────


def test_get_reviews_for_place_returns_in_newest_first_order(repo: ReviewRepository):
    rs = _mk_reviewset(
        reviews=[
            _mk_review("r1", publish_offset_days=10),  # newest
            _mk_review("r2", publish_offset_days=60),  # oldest
            _mk_review("r3", publish_offset_days=30),  # middle
        ]
    )
    repo.save_capture(rs, capture_id="cap-1")

    out = repo.get_reviews_for_place("ChIJ_X")
    assert [r.review_id for r in out] == ["r1", "r3", "r2"]


def test_get_reviews_for_place_with_unknown_publish_at_sorts_last(
    repo: ReviewRepository,
):
    """Reviews with NULL approx_publish_at should sort after dated ones."""
    r_dated = _mk_review("r_dated", publish_offset_days=30)
    r_unknown = _mk_review("r_unknown")
    r_unknown.approx_publish_at = None  # type: ignore[assignment]
    rs = _mk_reviewset(reviews=[r_unknown, r_dated])
    repo.save_capture(rs, capture_id="cap-1")

    out = repo.get_reviews_for_place("ChIJ_X")
    assert out[-1].review_id == "r_unknown"


def test_get_reviews_for_place_filters_by_place_id(repo: ReviewRepository):
    repo.save_capture(
        _mk_reviewset(place_id="ChIJ_A", reviews=[_mk_review("r1", place_id="ChIJ_A")]),
        capture_id="cap-A",
    )
    repo.save_capture(
        _mk_reviewset(place_id="ChIJ_B", reviews=[_mk_review("r2", place_id="ChIJ_B")]),
        capture_id="cap-B",
    )

    only_a = repo.get_reviews_for_place("ChIJ_A")
    only_b = repo.get_reviews_for_place("ChIJ_B")
    assert {r.review_id for r in only_a} == {"r1"}
    assert {r.review_id for r in only_b} == {"r2"}


def test_get_latest_capture_returns_most_recent(repo: ReviewRepository):
    repo.save_capture(_mk_reviewset(captured_at=_T1), capture_id="old")
    repo.save_capture(
        _mk_reviewset(captured_at=_T3, reviews=[]), capture_id="new",
    )

    latest = repo.get_latest_capture("ChIJ_X")
    assert latest is not None
    assert latest["capture_id"] == "new"


def test_get_latest_capture_returns_none_when_no_captures(repo: ReviewRepository):
    assert repo.get_latest_capture("ChIJ_NOPE") is None


def test_count_captures_for_place(repo: ReviewRepository):
    repo.save_capture(_mk_reviewset(captured_at=_T1), capture_id="c1")
    repo.save_capture(_mk_reviewset(captured_at=_T2, reviews=[]), capture_id="c2")
    repo.save_capture(_mk_reviewset(captured_at=_T3, reviews=[]), capture_id="c3")
    assert repo.count_captures_for_place("ChIJ_X") == 3


def test_get_known_review_ids_for_place(repo: ReviewRepository):
    """Used by the controller for incremental refresh."""
    rs = _mk_reviewset(reviews=[_mk_review("r1"), _mk_review("r2"), _mk_review("r3")])
    repo.save_capture(rs, capture_id="cap-1")

    known = repo.get_known_review_ids_for_place("ChIJ_X")
    assert known == {"r1", "r2", "r3"}


def test_get_known_review_ids_returns_empty_for_unknown_place(repo: ReviewRepository):
    assert repo.get_known_review_ids_for_place("ChIJ_NOPE") == set()


# ── round-trip fidelity ────────────────────────────────────────────────


def test_review_round_trips_with_full_metadata(repo: ReviewRepository):
    r_in = _mk_review(
        "r_full",
        text="A long thoughtful review",
        owner_response="Thank you so much",
        photo_urls=["https://lh3/photo1", "https://lh3/photo2"],
        likes_count=7,
    )
    repo.save_capture(_mk_reviewset(reviews=[r_in]), capture_id="cap-1")

    out = repo.get_reviews_for_place("ChIJ_X")
    assert len(out) == 1
    r = out[0]
    assert r.review_id == "r_full"
    assert r.text == "A long thoughtful review"
    assert r.owner_response_text == "Thank you so much"
    assert r.owner_response_relative_time == "1 month ago"
    assert r.owner_response_approx_at is not None
    assert r.photo_urls == ["https://lh3/photo1", "https://lh3/photo2"]
    assert r.likes_count == 7
    assert r.rating == 5


def test_review_with_empty_optionals_round_trips(repo: ReviewRepository):
    """A sparse review (e.g. no text, no owner response) should round-trip
    with all optional fields preserved as None or empty list."""
    r = Review(review_id="sparse", place_id="ChIJ_X")
    repo.save_capture(_mk_reviewset(reviews=[r]), capture_id="cap-1")

    out = repo.get_reviews_for_place("ChIJ_X")
    assert len(out) == 1
    rr = out[0]
    assert rr.review_id == "sparse"
    assert rr.text is None
    assert rr.rating is None
    assert rr.owner_response_text is None
    assert rr.photo_urls == []
    assert rr.raw_json == {}


def test_capture_metadata_round_trips(repo: ReviewRepository):
    rs = _mk_reviewset(
        total_per_gbp=2500,
        cap=1000,
        order="newest_first",
        fetch_status="partial",
        reviews=[_mk_review("r1"), _mk_review("r2")],
    )
    rs.error_message = "stopped after scroll #4"
    repo.save_capture(rs, capture_id="cap-1")

    cap = repo.get_latest_capture("ChIJ_X")
    assert cap is not None
    assert cap["total_reviews_per_gbp"] == 2500
    assert cap["capture_cap"] == 1000
    assert cap["capture_order"] == "newest_first"
    assert cap["fetch_status"] == "partial"
    assert cap["error_message"] == "stopped after scroll #4"
    assert cap["is_truncated"] == 1  # 2 < 2500
    assert cap["reviews_captured"] == 2
