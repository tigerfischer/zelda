"""Tests for enrichment source adapters.

Each adapter is a thin shim — most tests mock its underlying
controller and exercise the cache logic + dict shape directly.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from zelda.controllers.enrichment_sources import (
    BLOCKED_STATUSES,
    ERROR_STATUSES,
    GoogleReviewsSourceAdapter,
    PractoSourceAdapter,
    SUCCESSFUL_STATUSES,
    SourceAdapter,
)
from zelda.models.practo_profile import PractoProfile
from zelda.models.google_places_lead import GooglePlacesLead
from zelda.models.review import Review, ReviewSet
from zelda.repositories.practo_profile_repo import PractoProfileRepository
from zelda.repositories.review_repo import ReviewRepository


_T1 = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
_T_NOW = _T1 + timedelta(days=10)
_T_LONG_AGO = _T1 - timedelta(days=200)


# ── helpers + fixtures ──────────────────────────────────────────────────


def _mk_lead(place_id: str = "ChIJ_X", **overrides) -> GooglePlacesLead:
    base = dict(
        place_id=place_id,
        city="Ludhiana",
        name="Test Clinic",
        review_count=100,
        discovered_at=_T1,
        last_modified_at=_T1,
    )
    base.update(overrides)
    return GooglePlacesLead(**base)


def _mk_review_set(place_id="p1", n=3, captured_at=_T_NOW, status="ok") -> ReviewSet:
    reviews = [
        Review(review_id=f"r{i}", place_id=place_id, rating=5, text=f"r{i}")
        for i in range(n)
    ]
    return ReviewSet(
        place_id=place_id,
        reviews=reviews,
        total_reviews_per_gbp=100,
        capture_cap=1000,
        capture_order="newest_first",
        captured_at=captured_at,
        fetch_status=status,  # type: ignore[arg-type]
    )


@pytest.fixture
def review_repo():
    r = ReviewRepository(":memory:")
    yield r
    r.close()


@pytest.fixture
def practo_repo():
    r = PractoProfileRepository(":memory:")
    yield r
    r.close()


# ── status set sanity ───────────────────────────────────────────────────


def test_status_sets_are_disjoint():
    """Each fetch_status should fall into exactly one bucket so the
    orchestrator's interpretation is unambiguous."""
    all_sets = [
        SUCCESSFUL_STATUSES,
        BLOCKED_STATUSES,
        ERROR_STATUSES,
    ]
    for i, a in enumerate(all_sets):
        for b in all_sets[i + 1:]:
            assert not (a & b), f"overlap between {a} and {b}"


# ── GoogleReviewsSourceAdapter ─────────────────────────────────────────


def test_reviews_adapter_name():
    adapter = GoogleReviewsSourceAdapter(MagicMock(), MagicMock())
    assert adapter.name == "google_reviews"


def test_reviews_adapter_can_fetch_is_always_true(review_repo):
    """Reviews need only place_id + name + city, all on GooglePlacesLead."""
    adapter = GoogleReviewsSourceAdapter(MagicMock(), review_repo)
    assert adapter.can_fetch(_mk_lead("p1")) is True
    assert adapter.can_fetch(_mk_lead("p2")) is True


def test_reviews_adapter_is_cached_fresh_false_without_capture(review_repo):
    adapter = GoogleReviewsSourceAdapter(MagicMock(), review_repo)
    assert adapter.is_cached_fresh("p1", max_age_days=180, now=_T_NOW) is False


def test_reviews_adapter_is_cached_fresh_true_when_recent_ok_capture(review_repo):
    review_repo.save_capture(
        _mk_review_set("p1", captured_at=_T_NOW - timedelta(days=30), status="ok"),
        capture_id="cap-1",
    )
    adapter = GoogleReviewsSourceAdapter(MagicMock(), review_repo)
    assert adapter.is_cached_fresh("p1", max_age_days=180, now=_T_NOW) is True


def test_reviews_adapter_is_cached_fresh_false_when_old_capture(review_repo):
    review_repo.save_capture(
        _mk_review_set("p1", captured_at=_T_LONG_AGO, status="ok"),
        capture_id="cap-old",
    )
    adapter = GoogleReviewsSourceAdapter(MagicMock(), review_repo)
    # 200 days old, max_age=180 → stale
    assert adapter.is_cached_fresh("p1", max_age_days=180, now=_T_NOW) is False


def test_reviews_adapter_is_cached_fresh_false_when_capture_was_blocked(review_repo):
    review_repo.save_capture(
        _mk_review_set("p1", captured_at=_T_NOW - timedelta(days=1), status="blocked"),
        capture_id="cap-blk",
    )
    adapter = GoogleReviewsSourceAdapter(MagicMock(), review_repo)
    # Recent but the fetch was blocked — not a cache hit
    assert adapter.is_cached_fresh("p1", max_age_days=180, now=_T_NOW) is False


def test_reviews_adapter_partial_status_counts_as_fresh(review_repo):
    """A partial capture (some reviews captured before an error) is
    still useful data — treat as a successful cache hit."""
    review_repo.save_capture(
        _mk_review_set("p1", captured_at=_T_NOW - timedelta(days=1), status="partial"),
        capture_id="cap-part",
    )
    adapter = GoogleReviewsSourceAdapter(MagicMock(), review_repo)
    assert adapter.is_cached_fresh("p1", max_age_days=180, now=_T_NOW) is True


def test_reviews_adapter_fetch_for_lead_delegates_to_controller(review_repo):
    """The adapter passes through process_one_lead and adds source name."""
    controller = MagicMock()
    controller.process_one_lead.return_value = {
        "place_id": "p1",
        "fetch_status": "ok",
        "reviews_captured": 5,
        "is_truncated": False,
        "capture_id": "cap-1",
        "artifact_path": "/tmp/x.jsonl",
        "error_message": None,
        "extra_errors": [],
    }
    adapter = GoogleReviewsSourceAdapter(
        controller, review_repo, max_reviews_per_place=500,
    )

    summary = adapter.fetch_for_lead(_mk_lead("p1"), capture_id="cap-1", now=_T_NOW)

    controller.process_one_lead.assert_called_once()
    call_kwargs = controller.process_one_lead.call_args.kwargs
    assert call_kwargs["capture_id"] == "cap-1"
    assert call_kwargs["max_reviews"] == 500
    assert summary["source"] == "google_reviews"
    assert summary["fetch_status"] == "ok"


# ── PractoSourceAdapter ────────────────────────────────────────────────


def _make_practo_adapter(
    enrich_controller=None,
    repo=None,
):
    """Helper: build a PractoSourceAdapter with sensible MagicMock
    defaults for any arg not supplied."""
    return PractoSourceAdapter(
        enrich_controller=enrich_controller or MagicMock(),
        practo_repo=repo or MagicMock(),
    )


def test_practo_adapter_name():
    assert _make_practo_adapter().name == "practo_profile"


def test_practo_adapter_can_fetch_false_without_row(practo_repo):
    """can_fetch is False when no row exists — discovery is now a
    pre-step, not part of this adapter."""
    adapter = _make_practo_adapter(repo=practo_repo)
    assert adapter.can_fetch(_mk_lead("p_no_data_anywhere")) is False


def test_practo_adapter_can_fetch_true_with_url_stub(practo_repo):
    practo_repo.upsert_stub("p1", "https://www.practo.com/known")
    adapter = _make_practo_adapter(repo=practo_repo)
    assert adapter.can_fetch(_mk_lead("p1")) is True


def test_practo_adapter_can_fetch_false_for_no_url_found_row(practo_repo):
    """no_url_found rows have empty practo_url — can_fetch False."""
    prof = PractoProfile(
        place_id="p1", practo_url="", fetch_status="no_url_found",
        fetched_at=_T_NOW, discovered_at=_T_NOW, last_modified_at=_T_NOW,
    )
    practo_repo.upsert(prof)
    adapter = _make_practo_adapter(repo=practo_repo)
    assert adapter.can_fetch(_mk_lead("p1")) is False


def test_practo_adapter_is_cached_fresh_false_without_row(practo_repo):
    adapter = _make_practo_adapter(repo=practo_repo)
    assert adapter.is_cached_fresh("p1", max_age_days=180, now=_T_NOW) is False


def test_practo_adapter_is_cached_fresh_false_for_pending(practo_repo):
    practo_repo.upsert_stub("p1", "https://www.practo.com/x")
    adapter = _make_practo_adapter(repo=practo_repo)
    # status='pending' = stub exists but never fetched → not fresh
    assert adapter.is_cached_fresh("p1", max_age_days=180, now=_T_NOW) is False


def test_practo_adapter_is_cached_fresh_false_for_blocked_or_error(practo_repo):
    for status in ("blocked", "error"):
        practo_repo.upsert_stub("p1", "https://www.practo.com/x")
        prof = PractoProfile(
            place_id="p1",
            practo_url="https://www.practo.com/x",
            fetch_status=status,
            fetched_at=_T_NOW - timedelta(days=1),
            discovered_at=_T1,
            last_modified_at=_T_NOW - timedelta(days=1),
        )
        practo_repo.upsert(prof)
        adapter = _make_practo_adapter(repo=practo_repo)
        assert adapter.is_cached_fresh("p1", max_age_days=180, now=_T_NOW) is False, status


def test_practo_adapter_is_cached_fresh_true_for_recent_ok(practo_repo):
    practo_repo.upsert_stub("p1", "https://www.practo.com/x")
    prof = PractoProfile(
        place_id="p1",
        practo_url="https://www.practo.com/x",
        fetch_status="ok",
        fetched_at=_T_NOW - timedelta(days=30),
        discovered_at=_T1,
        last_modified_at=_T_NOW - timedelta(days=30),
    )
    practo_repo.upsert(prof)
    adapter = _make_practo_adapter(repo=practo_repo)
    assert adapter.is_cached_fresh("p1", max_age_days=180, now=_T_NOW) is True


def test_practo_adapter_is_cached_fresh_false_for_old_ok(practo_repo):
    practo_repo.upsert_stub("p1", "https://www.practo.com/x")
    prof = PractoProfile(
        place_id="p1",
        practo_url="https://www.practo.com/x",
        fetch_status="ok",
        fetched_at=_T_LONG_AGO,
        discovered_at=_T1,
        last_modified_at=_T_LONG_AGO,
    )
    practo_repo.upsert(prof)
    adapter = _make_practo_adapter(repo=practo_repo)
    # 200 days old vs 180 max → stale
    assert adapter.is_cached_fresh("p1", max_age_days=180, now=_T_NOW) is False


def test_practo_adapter_not_found_is_terminal_fresh_forever(practo_repo):
    """`not_found` is a Practo terminal status — the URL is dead. We
    should NOT keep retrying on every run, ever."""
    practo_repo.upsert_stub("p1", "https://www.practo.com/dead")
    prof = PractoProfile(
        place_id="p1",
        practo_url="https://www.practo.com/dead",
        fetch_status="not_found",
        fetched_at=_T_LONG_AGO,
        discovered_at=_T1,
        last_modified_at=_T_LONG_AGO,
    )
    practo_repo.upsert(prof)
    adapter = _make_practo_adapter(repo=practo_repo)
    assert adapter.is_cached_fresh("p1", max_age_days=180, now=_T_NOW) is True


def test_practo_adapter_no_url_found_is_also_terminal_fresh(practo_repo):
    """`no_url_found` (discovery couldn't match) is similarly terminal
    — don't keep searching every run."""
    prof = PractoProfile(
        place_id="p1",
        practo_url="",
        fetch_status="no_url_found",
        fetched_at=_T_LONG_AGO,
        discovered_at=_T1,
        last_modified_at=_T_LONG_AGO,
    )
    practo_repo.upsert(prof)
    adapter = _make_practo_adapter(repo=practo_repo)
    assert adapter.is_cached_fresh("p1", max_age_days=180, now=_T_NOW) is True


# ── fetch_for_lead: enrichment-only (discovery is a pre-step now) ──────


def test_practo_adapter_fetch_runs_enrich_when_stub_exists(practo_repo):
    practo_repo.upsert_stub("p1", "https://www.practo.com/known")
    enrich = MagicMock()
    enrich.enrich_one.return_value = PractoProfile(
        place_id="p1", practo_url="https://www.practo.com/known",
        fetch_status="ok",
        fetched_at=_T_NOW, discovered_at=_T1, last_modified_at=_T_NOW,
    )

    adapter = PractoSourceAdapter(
        enrich_controller=enrich, practo_repo=practo_repo,
    )
    summary = adapter.fetch_for_lead(_mk_lead("p1"), capture_id="cap-1", now=_T_NOW)

    enrich.enrich_one.assert_called_once_with("p1")
    assert summary["fetch_status"] == "ok"


def test_practo_adapter_fetch_returns_no_url_found_when_no_row(practo_repo):
    """Defensive: if the matcher pre-step was skipped and no row
    exists, treat as no_url_found so the orchestrator counts cleanly."""
    enrich = MagicMock()

    adapter = PractoSourceAdapter(
        enrich_controller=enrich, practo_repo=practo_repo,
    )
    summary = adapter.fetch_for_lead(_mk_lead("p1"), capture_id="cap-x", now=_T_NOW)

    assert summary["fetch_status"] == "no_url_found"
    enrich.enrich_one.assert_not_called()


def test_practo_adapter_fetch_handles_enrich_exception(practo_repo):
    practo_repo.upsert_stub("p1", "https://www.practo.com/known")
    enrich = MagicMock()
    enrich.enrich_one.side_effect = RuntimeError("network died")

    adapter = PractoSourceAdapter(
        enrich_controller=enrich, practo_repo=practo_repo,
    )
    summary = adapter.fetch_for_lead(_mk_lead("p1"), capture_id="cap-1", now=_T_NOW)

    assert summary["fetch_status"] == "error"
    assert "network died" in (summary["error_message"] or "")
    assert summary["source"] == "practo_profile"


def test_practo_adapter_fetch_returns_error_when_enrich_returns_none(practo_repo):
    practo_repo.upsert_stub("p1", "https://www.practo.com/known")
    enrich = MagicMock()
    enrich.enrich_one.return_value = None

    adapter = PractoSourceAdapter(
        enrich_controller=enrich, practo_repo=practo_repo,
    )
    summary = adapter.fetch_for_lead(_mk_lead("p1"), capture_id="cap-1", now=_T_NOW)

    assert summary["fetch_status"] == "error"
    assert "returned None" in (summary["error_message"] or "")


def test_practo_adapter_fetch_skips_enrich_for_terminal_existing_row(practo_repo):
    """If a row already exists with terminal status (not_found /
    no_url_found), don't run enrich. The orchestrator's
    is_cached_fresh would normally short-circuit before fetch_for_lead
    but defend against the path where it doesn't (e.g. force_refresh)."""
    prof = PractoProfile(
        place_id="p1", practo_url="", fetch_status="no_url_found",
        fetched_at=_T_LONG_AGO, discovered_at=_T1, last_modified_at=_T_LONG_AGO,
    )
    practo_repo.upsert(prof)
    enrich = MagicMock()

    adapter = PractoSourceAdapter(
        enrich_controller=enrich, practo_repo=practo_repo,
    )
    summary = adapter.fetch_for_lead(_mk_lead("p1"), capture_id="cap-1", now=_T_NOW)

    enrich.enrich_one.assert_not_called()
    assert summary["fetch_status"] == "no_url_found"


# ── Protocol conformance ───────────────────────────────────────────────


def test_both_adapters_satisfy_source_adapter_protocol():
    """Quick structural check: both classes should pass isinstance(...,
    SourceAdapter) at runtime via @runtime_checkable."""
    rev = GoogleReviewsSourceAdapter(MagicMock(), MagicMock())
    pra = _make_practo_adapter()
    assert isinstance(rev, SourceAdapter)
    assert isinstance(pra, SourceAdapter)
