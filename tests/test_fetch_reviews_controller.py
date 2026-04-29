import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from zelda.controllers.fetch_reviews import (
    FetchReviewsController,
    FetchReviewsResult,
    _build_search_query,
)
from zelda.models.raw_lead import RawLead
from zelda.models.review import Review, ReviewSet
from zelda.repositories.raw_lead_repo import RawLeadRepository
from zelda.repositories.review_repo import ReviewRepository


_T1 = datetime(2026, 4, 29, 10, 0, 0, tzinfo=timezone.utc)
_T_LATER = _T1 + timedelta(hours=1)


# ── fakes ───────────────────────────────────────────────────────────────


class FakeReviewsGateway:
    """In-memory stand-in for `GoogleReviewsGateway`. Tracks calls so
    tests can assert on the controller's orchestration."""

    def __init__(self) -> None:
        # Per place_id: ReviewSet to return, OR an Exception to raise.
        self._responses: dict[str, ReviewSet] = {}
        self._failures: dict[str, Exception] = {}
        self.fetch_calls: list[dict[str, Any]] = []
        self.context_resets: int = 0

    def set_response(self, place_id: str, review_set: ReviewSet) -> None:
        self._responses[place_id] = review_set

    def set_failure(self, place_id: str, exc: Exception) -> None:
        self._failures[place_id] = exc

    def fetch_reviews(
        self,
        place_id: str,
        *,
        search_query: str,
        max_reviews: int = 1000,
        order: str = "newest_first",
        total_reviews_hint: int | None = None,
    ) -> ReviewSet:
        self.fetch_calls.append({
            "place_id": place_id,
            "search_query": search_query,
            "max_reviews": max_reviews,
            "order": order,
            "total_reviews_hint": total_reviews_hint,
        })
        if place_id in self._failures:
            raise self._failures[place_id]
        if place_id in self._responses:
            return self._responses[place_id]
        # Default: empty OK response so unconfigured calls don't crash
        return ReviewSet(
            place_id=place_id,
            reviews=[],
            total_reviews_per_gbp=total_reviews_hint,
            capture_cap=max_reviews,
            capture_order=order,
            captured_at=_T_LATER,
            fetch_status="ok",
        )

    def reset_context(self) -> None:
        self.context_resets += 1


# ── helpers ─────────────────────────────────────────────────────────────


def _mk_lead(
    place_id: str = "ChIJ_X",
    *,
    name: str = "Test Clinic",
    city: str = "Ludhiana",
    review_count: int = 50,
) -> RawLead:
    return RawLead(
        place_id=place_id,
        city=city,
        name=name,
        review_count=review_count,
        discovered_at=_T1,
        last_modified_at=_T1,
    )


def _mk_review(rid: str = "r1", place_id: str = "ChIJ_X") -> Review:
    return Review(
        review_id=rid,
        place_id=place_id,
        rating=5,
        text=f"review {rid}",
        author_name=f"user_{rid}",
        relative_publish_time="a month ago",
        approx_publish_at=_T1 - timedelta(days=30),
        sequence_in_capture=1,
    )


def _mk_review_set(
    place_id: str = "ChIJ_X",
    n_reviews: int = 3,
    *,
    fetch_status: str = "ok",
    captured_at: datetime = _T_LATER,
    total_per_gbp: int | None = 50,
) -> ReviewSet:
    reviews = [_mk_review(f"r{i}", place_id=place_id) for i in range(n_reviews)]
    return ReviewSet(
        place_id=place_id,
        reviews=reviews,
        total_reviews_per_gbp=total_per_gbp,
        capture_cap=1000,
        capture_order="newest_first",
        captured_at=captured_at,
        earliest_review_at=min(
            (r.approx_publish_at for r in reviews if r.approx_publish_at),
            default=None,
        ),
        latest_review_at=max(
            (r.approx_publish_at for r in reviews if r.approx_publish_at),
            default=None,
        ),
        fetch_status=fetch_status,  # type: ignore[arg-type]
    )


# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def gateway() -> FakeReviewsGateway:
    return FakeReviewsGateway()


@pytest.fixture
def review_repo():
    r = ReviewRepository(":memory:")
    yield r
    r.close()


@pytest.fixture
def lead_repo():
    r = RawLeadRepository(":memory:")
    yield r
    r.close()


@pytest.fixture
def controller(
    gateway: FakeReviewsGateway,
    review_repo: ReviewRepository,
    lead_repo: RawLeadRepository,
    tmp_path: Path,
) -> FetchReviewsController:
    sleep_calls: list[float] = []
    return FetchReviewsController(
        gateway=gateway,
        review_repo=review_repo,
        lead_repo=lead_repo,
        artifacts_dir=tmp_path / "reviews",
        clock=lambda: _T_LATER,
        sleeper=sleep_calls.append,  # never blocks tests
        inter_place_delay_range=(0.0, 0.0),
        context_reset_interval=0,  # disable in tests by default
    )


# ── input validation ───────────────────────────────────────────────────


def test_run_rejects_blank_city(controller):
    with pytest.raises(ValueError, match="city"):
        controller.run("")
    with pytest.raises(ValueError, match="city"):
        controller.run("   ")


def test_run_rejects_negative_max_places(controller):
    with pytest.raises(ValueError, match="max_places"):
        controller.run("Ludhiana", max_places=-1)


def test_run_rejects_zero_max_reviews_per_place(controller):
    with pytest.raises(ValueError, match="max_reviews_per_place"):
        controller.run("Ludhiana", max_reviews_per_place=0)


def test_run_rejects_negative_refresh_min_age_days(controller):
    with pytest.raises(ValueError, match="refresh_min_age_days"):
        controller.run("Ludhiana", refresh_min_age_days=-1.0)


# ── empty / no-op ──────────────────────────────────────────────────────


def test_run_with_no_leads_for_city_is_noop(controller, gateway, review_repo):
    """No leads in the city → controller should return a clean result
    with all-zero stats and no gateway calls."""
    result = controller.run("Ludhiana")
    assert result.n_leads_in_city == 0
    assert result.n_eligible == 0
    assert result.n_processed == 0
    assert gateway.fetch_calls == []


# ── happy path ─────────────────────────────────────────────────────────


def test_run_processes_each_lead_in_city(
    controller, gateway, review_repo, lead_repo,
):
    lead_repo.upsert_many([_mk_lead("p1"), _mk_lead("p2"), _mk_lead("p3")])
    for pid in ("p1", "p2", "p3"):
        gateway.set_response(pid, _mk_review_set(pid))

    result = controller.run("Ludhiana")

    assert result.n_leads_in_city == 3
    assert result.n_eligible == 3
    assert result.n_processed == 3
    assert result.n_successful == 3
    assert {c["place_id"] for c in gateway.fetch_calls} == {"p1", "p2", "p3"}
    assert result.n_total_reviews_captured == 9  # 3 reviews × 3 places


def test_run_persists_captures_to_review_repo(
    controller, gateway, review_repo, lead_repo,
):
    lead_repo.upsert_many([_mk_lead("p1")])
    gateway.set_response("p1", _mk_review_set("p1", n_reviews=5))

    controller.run("Ludhiana")

    assert review_repo.count_captures_for_place("p1") == 1
    assert review_repo.count_reviews_for_place("p1") == 5


def test_run_writes_jsonl_artifact_per_capture(
    controller, gateway, review_repo, lead_repo, tmp_path,
):
    lead_repo.upsert_many([_mk_lead("p1")])
    gateway.set_response("p1", _mk_review_set("p1", n_reviews=4))

    result = controller.run("Ludhiana")

    artifact_path_str = result.captures[0]["artifact_path"]
    assert artifact_path_str is not None
    artifact = Path(artifact_path_str)
    assert artifact.exists()
    lines = artifact.read_text().strip().split("\n")
    assert len(lines) == 4
    parsed = [json.loads(line) for line in lines]
    assert {p["review_id"] for p in parsed} == {"r0", "r1", "r2", "r3"}


def test_run_skips_artifact_when_no_reviews(
    controller, gateway, review_repo, lead_repo,
):
    """Empty capture (no reviews returned) → no artifact file written,
    but the capture metadata IS still persisted to review_captures."""
    lead_repo.upsert_many([_mk_lead("p1")])
    gateway.set_response("p1", _mk_review_set("p1", n_reviews=0))

    result = controller.run("Ludhiana")

    assert result.captures[0]["artifact_path"] is None
    assert review_repo.count_captures_for_place("p1") == 1
    assert review_repo.count_reviews_for_place("p1") == 0


def test_run_passes_total_reviews_hint_from_lead(controller, gateway, lead_repo):
    lead_repo.upsert_many([_mk_lead("p1", review_count=727)])
    controller.run("Ludhiana")
    assert gateway.fetch_calls[0]["total_reviews_hint"] == 727


def test_run_passes_max_reviews_per_place_to_gateway(
    controller, gateway, lead_repo,
):
    lead_repo.upsert_many([_mk_lead("p1")])
    controller.run("Ludhiana", max_reviews_per_place=250)
    assert gateway.fetch_calls[0]["max_reviews"] == 250


def test_run_builds_search_query_from_name_and_city(controller, gateway, lead_repo):
    lead_repo.upsert_many(
        [_mk_lead("p1", name="Sai Dental Clinic", city="Ludhiana")],
    )
    controller.run("Ludhiana")
    assert gateway.fetch_calls[0]["search_query"] == "Sai Dental Clinic Ludhiana"


# ── search query builder helper ────────────────────────────────────────


def test_build_search_query_simple_case():
    assert _build_search_query("Sai Dental Clinic", "Ludhiana") == "Sai Dental Clinic Ludhiana"


def test_build_search_query_normalizes_unicode_stylized_chars():
    """Many GBP names use math-italic or sans-serif-bold variants for
    SEO theatre. NFKD normalize back to plain Latin chars."""
    name = "𝗦𝗮𝗶 𝗗𝗲𝗻𝘁𝗮𝗹 𝗖𝗹𝗶𝗻𝗶𝗰"  # math-sans-serif-bold
    out = _build_search_query(name, "Ludhiana")
    assert "𝗦" not in out
    assert "Sai Dental Clinic" in out
    assert "Ludhiana" in out


def test_build_search_query_strips_seo_suffix_after_dash():
    out = _build_search_query(
        "Sai Dental Clinic - Best Dentist Near Me in Ludhiana",
        "Ludhiana",
    )
    assert out == "Sai Dental Clinic Ludhiana"
    assert "Best Dentist" not in out


def test_build_search_query_strips_seo_suffix_after_pipe():
    out = _build_search_query(
        "Saggar Dental | Implant OPG & CBCT Centre",
        "Ludhiana",
    )
    assert "|" not in out
    assert "Implant" not in out
    assert "Saggar Dental Ludhiana" in out


def test_build_search_query_strips_seo_suffix_after_em_dash():
    out = _build_search_query("Foo Dental — Premier Care", "Mumbai")
    assert "Foo Dental Mumbai" == out


def test_build_search_query_does_not_double_city():
    """If the cleaned name already contains the city, don't re-append."""
    out = _build_search_query("Smile Dental Ludhiana", "Ludhiana")
    assert out == "Smile Dental Ludhiana"
    assert out.lower().count("ludhiana") == 1


def test_build_search_query_collapses_whitespace():
    out = _build_search_query("Foo   Dental    Clinic", "Mumbai")
    assert out == "Foo Dental Clinic Mumbai"


def test_build_search_query_handles_empty_city():
    out = _build_search_query("Foo Dental", "")
    assert out == "Foo Dental"


def test_build_search_query_real_world_sai_dental_case():
    """The exact stored name that broke the first live CLI run.
    Combination of math-bold unicode + SEO suffix + already-includes-city."""
    raw = "𝗦𝗮𝗶 𝗗𝗲𝗻𝘁𝗮𝗹 𝗖𝗹𝗶𝗻𝗶𝗰 - Best Dentist Near Me in Ludhiana"
    out = _build_search_query(raw, "Ludhiana")
    assert out == "Sai Dental Clinic Ludhiana"


# ── process_one_lead — the public per-lead path used by orchestrator ───


def test_process_one_lead_returns_capture_dict_for_success(
    controller, gateway, lead_repo, tmp_path,
):
    lead = _mk_lead("p1")
    gateway.set_response("p1", _mk_review_set("p1", n_reviews=4))

    summary = controller.process_one_lead(
        lead, capture_id="cap-1", max_reviews=100,
    )

    assert summary["place_id"] == "p1"
    assert summary["fetch_status"] == "ok"
    assert summary["reviews_captured"] == 4
    assert summary["capture_id"] == "cap-1"
    assert summary["artifact_path"] is not None
    assert summary["error_message"] is None
    assert summary["extra_errors"] == []
    # Artifact file exists
    assert Path(summary["artifact_path"]).exists()


def test_process_one_lead_returns_error_dict_on_gateway_exception(
    controller, gateway,
):
    lead = _mk_lead("p_bad")
    gateway.set_failure("p_bad", RuntimeError("network died"))

    summary = controller.process_one_lead(
        lead, capture_id="cap-bad", max_reviews=100,
    )

    assert summary["fetch_status"] == "error"
    assert summary["reviews_captured"] == 0
    assert "network died" in (summary["error_message"] or "")
    assert summary["artifact_path"] is None


def test_process_one_lead_returns_blocked_status_without_aborting(
    controller, gateway,
):
    """`process_one_lead` is pure per-lead — it does NOT set the
    aborted-due-to-block flag (that's the city loop's job). Just
    returns the blocked status faithfully."""
    lead = _mk_lead("p1")
    blocked = _mk_review_set("p1", n_reviews=0, fetch_status="blocked")
    blocked.error_message = "captcha"
    gateway.set_response("p1", blocked)

    summary = controller.process_one_lead(
        lead, capture_id="cap-block", max_reviews=100,
    )

    assert summary["fetch_status"] == "blocked"
    assert summary["error_message"] == "captcha"


def test_process_one_lead_uses_default_artifact_dir_when_not_given(
    controller, gateway,
):
    lead = _mk_lead("p1")
    gateway.set_response("p1", _mk_review_set("p1", n_reviews=2))

    summary = controller.process_one_lead(
        lead, capture_id="cap-default-dir", max_reviews=100,
    )

    # Default = self._artifacts_dir / slug(city) / capture_id.jsonl
    assert summary["artifact_path"] is not None
    assert "ludhiana" in summary["artifact_path"]


def test_process_one_lead_persists_to_review_repo(
    controller, gateway, review_repo,
):
    lead = _mk_lead("p1")
    gateway.set_response("p1", _mk_review_set("p1", n_reviews=3))

    controller.process_one_lead(lead, capture_id="cap-persist", max_reviews=100)

    assert review_repo.count_captures_for_place("p1") == 1
    assert review_repo.count_reviews_for_place("p1") == 3


# ── refresh / recency filter ───────────────────────────────────────────


def test_run_skips_places_captured_recently(
    controller, gateway, review_repo, lead_repo,
):
    """A place captured 1 day ago should be skipped at default
    refresh_min_age_days=7."""
    lead_repo.upsert_many([_mk_lead("p1"), _mk_lead("p2")])
    # p1 was captured yesterday — should be skipped
    recent_capture = _mk_review_set(
        "p1", captured_at=_T_LATER - timedelta(days=1),
    )
    review_repo.save_capture(recent_capture, capture_id="cap-old-p1")
    gateway.set_response("p2", _mk_review_set("p2"))

    result = controller.run("Ludhiana")

    assert result.n_skipped_recent == 1
    assert result.n_eligible == 1
    assert result.n_processed == 1
    assert {c["place_id"] for c in gateway.fetch_calls} == {"p2"}


def test_run_does_not_skip_when_old_enough(
    controller, gateway, review_repo, lead_repo,
):
    lead_repo.upsert_many([_mk_lead("p1")])
    # p1's last capture was 30 days ago — older than the 7-day default
    old_capture = _mk_review_set(
        "p1", captured_at=_T_LATER - timedelta(days=30),
    )
    review_repo.save_capture(old_capture, capture_id="cap-very-old")
    gateway.set_response("p1", _mk_review_set("p1"))

    result = controller.run("Ludhiana")

    assert result.n_skipped_recent == 0
    assert result.n_eligible == 1
    assert result.n_processed == 1


def test_run_force_refresh_bypasses_recency_filter(
    controller, gateway, review_repo, lead_repo,
):
    lead_repo.upsert_many([_mk_lead("p1")])
    recent = _mk_review_set("p1", captured_at=_T_LATER - timedelta(hours=1))
    review_repo.save_capture(recent, capture_id="cap-now")
    gateway.set_response("p1", _mk_review_set("p1"))

    result = controller.run("Ludhiana", force_refresh=True)

    assert result.n_skipped_recent == 0
    assert result.n_processed == 1


def test_run_refresh_window_is_configurable(
    controller, gateway, review_repo, lead_repo,
):
    lead_repo.upsert_many([_mk_lead("p1")])
    capture_3_days_ago = _mk_review_set(
        "p1", captured_at=_T_LATER - timedelta(days=3),
    )
    review_repo.save_capture(capture_3_days_ago, capture_id="cap-3d")
    gateway.set_response("p1", _mk_review_set("p1"))

    # With refresh_min_age=2.0, 3 days old is eligible
    result = controller.run("Ludhiana", refresh_min_age_days=2.0)
    assert result.n_processed == 1


# ── max_places cap ─────────────────────────────────────────────────────


def test_run_caps_at_max_places(controller, gateway, lead_repo):
    lead_repo.upsert_many([_mk_lead(f"p{i}") for i in range(5)])
    for i in range(5):
        gateway.set_response(f"p{i}", _mk_review_set(f"p{i}"))

    result = controller.run("Ludhiana", max_places=2)

    assert result.n_eligible == 5
    assert result.n_after_max_places == 2
    assert result.n_processed == 2
    assert len(gateway.fetch_calls) == 2


def test_run_max_places_zero_means_no_processing(controller, gateway, lead_repo):
    lead_repo.upsert_many([_mk_lead("p1"), _mk_lead("p2")])
    result = controller.run("Ludhiana", max_places=0)
    assert result.n_processed == 0
    assert gateway.fetch_calls == []


# ── error handling ─────────────────────────────────────────────────────


def test_gateway_exception_is_recorded_and_run_continues(
    controller, gateway, lead_repo,
):
    lead_repo.upsert_many([_mk_lead("p_bad"), _mk_lead("p_good")])
    gateway.set_failure("p_bad", RuntimeError("network died"))
    gateway.set_response("p_good", _mk_review_set("p_good"))

    result = controller.run("Ludhiana")

    assert result.n_processed == 2
    assert result.n_errored == 1
    assert result.n_successful == 1
    assert any("network died" in e for e in result.errors)


def test_blocked_status_aborts_the_run(controller, gateway, lead_repo):
    """If the gateway returns fetch_status='blocked'/'captcha', the
    controller must stop — the same block hits the next place too."""
    lead_repo.upsert_many([_mk_lead("p1"), _mk_lead("p2"), _mk_lead("p3")])
    gateway.set_response("p1", _mk_review_set("p1"))
    blocked = _mk_review_set("p2", n_reviews=0, fetch_status="blocked")
    blocked.error_message = "/sorry/ url"
    gateway.set_response("p2", blocked)
    gateway.set_response("p3", _mk_review_set("p3"))

    result = controller.run("Ludhiana")

    assert result.aborted_due_to_block is True
    assert result.n_blocked == 1
    assert len(gateway.fetch_calls) == 2  # p3 never attempted
    assert result.n_processed == 2


def test_partial_status_does_not_abort(controller, gateway, lead_repo):
    """fetch_status='partial' = some reviews captured before an error.
    NOT a block; the run should continue."""
    lead_repo.upsert_many([_mk_lead("p1"), _mk_lead("p2")])
    gateway.set_response(
        "p1", _mk_review_set("p1", fetch_status="partial"),
    )
    gateway.set_response("p2", _mk_review_set("p2"))

    result = controller.run("Ludhiana")

    assert result.aborted_due_to_block is False
    assert result.n_successful == 2
    assert len(gateway.fetch_calls) == 2


# ── inter-place rate limiting ──────────────────────────────────────────


def test_run_sleeps_between_places_but_not_before_first(
    gateway, review_repo, lead_repo, tmp_path,
):
    sleep_calls: list[float] = []
    ctrl = FetchReviewsController(
        gateway=gateway,
        review_repo=review_repo,
        lead_repo=lead_repo,
        artifacts_dir=tmp_path / "reviews",
        clock=lambda: _T_LATER,
        sleeper=sleep_calls.append,
        inter_place_delay_range=(5.0, 5.0),  # deterministic
        context_reset_interval=0,
    )
    lead_repo.upsert_many([_mk_lead(f"p{i}") for i in range(3)])
    for i in range(3):
        gateway.set_response(f"p{i}", _mk_review_set(f"p{i}"))

    ctrl.run("Ludhiana")

    # 3 places → 2 inter-place sleeps (between 1↔2 and 2↔3, not before 1)
    assert sleep_calls == [5.0, 5.0]


def test_run_resets_browser_context_periodically(
    gateway, review_repo, lead_repo, tmp_path,
):
    ctrl = FetchReviewsController(
        gateway=gateway,
        review_repo=review_repo,
        lead_repo=lead_repo,
        artifacts_dir=tmp_path / "reviews",
        clock=lambda: _T_LATER,
        sleeper=lambda _: None,
        inter_place_delay_range=(0.0, 0.0),
        context_reset_interval=2,  # reset every 2 places
    )
    lead_repo.upsert_many([_mk_lead(f"p{i}") for i in range(5)])
    for i in range(5):
        gateway.set_response(f"p{i}", _mk_review_set(f"p{i}"))

    ctrl.run("Ludhiana")

    # 5 places, reset every 2 → resets at boundaries (2, 4) → 2 resets
    assert gateway.context_resets == 2


def test_run_with_zero_context_reset_interval_never_resets(
    gateway, review_repo, lead_repo, tmp_path,
):
    ctrl = FetchReviewsController(
        gateway=gateway,
        review_repo=review_repo,
        lead_repo=lead_repo,
        artifacts_dir=tmp_path / "reviews",
        clock=lambda: _T_LATER,
        sleeper=lambda _: None,
        inter_place_delay_range=(0.0, 0.0),
        context_reset_interval=0,
    )
    lead_repo.upsert_many([_mk_lead(f"p{i}") for i in range(10)])
    for i in range(10):
        gateway.set_response(f"p{i}", _mk_review_set(f"p{i}"))

    ctrl.run("Ludhiana")

    assert gateway.context_resets == 0


# ── city filtering ─────────────────────────────────────────────────────


def test_run_filters_leads_by_city(controller, gateway, lead_repo):
    lead_repo.upsert_many([
        _mk_lead("p_lud", city="Ludhiana"),
        _mk_lead("p_mum", city="Mumbai"),
    ])
    gateway.set_response("p_lud", _mk_review_set("p_lud"))

    result = controller.run("Ludhiana")

    assert result.n_leads_in_city == 1
    assert {c["place_id"] for c in gateway.fetch_calls} == {"p_lud"}


# ── result shape ───────────────────────────────────────────────────────


def test_result_includes_capture_summaries_per_place(
    controller, gateway, lead_repo,
):
    lead_repo.upsert_many([_mk_lead("p1"), _mk_lead("p2")])
    gateway.set_response("p1", _mk_review_set("p1", n_reviews=5))
    gateway.set_response("p2", _mk_review_set("p2", n_reviews=2))

    result = controller.run("Ludhiana")

    assert len(result.captures) == 2
    by_pid = {c["place_id"]: c for c in result.captures}
    assert by_pid["p1"]["reviews_captured"] == 5
    assert by_pid["p2"]["reviews_captured"] == 2
    assert all(c["fetch_status"] == "ok" for c in result.captures)
    assert all(c["capture_id"].startswith(result.run_id) for c in result.captures)
