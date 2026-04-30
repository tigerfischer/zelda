"""Tests for `SyncPipeline` — orchestrator that runs N per-source
sync steps for one city. Mirrors the discovery pipeline tests in
shape; the sync-specific shape is the use of `SyncStepResult` with
its (pulled, inserted, updated) counters.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

import pytest

from zelda.controllers.sync_pipeline import (
    SyncPipeline,
    SyncStep,
    SyncStepResult,
)


_T_NOW = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)


class _FakeStep:
    def __init__(
        self,
        name: str,
        *,
        on_call: Callable[[str, str], SyncStepResult] | None = None,
    ) -> None:
        self._name = name
        self._on_call = on_call
        self.calls: list[tuple[str, str]] = []

    @property
    def name(self) -> str:
        return self._name

    def run_for_city(self, city: str, *, run_id: str) -> SyncStepResult:
        self.calls.append((city, run_id))
        if self._on_call is not None:
            return self._on_call(city, run_id)
        return SyncStepResult(
            step_name=self._name, city=city,
            started_at=_T_NOW, finished_at=_T_NOW,
            pulled=10, inserted=8, updated=2,
        )


def _ok_result(name: str, city: str, *, inserted: int = 5, updated: int = 0) -> SyncStepResult:
    return SyncStepResult(
        step_name=name, city=city,
        started_at=_T_NOW, finished_at=_T_NOW,
        pulled=inserted + updated, inserted=inserted, updated=updated,
    )


# ── construction validation ─────────────────────────────────────────


def test_pipeline_requires_at_least_one_step():
    with pytest.raises(ValueError, match="at least one"):
        SyncPipeline([])


def test_pipeline_rejects_duplicate_step_names():
    with pytest.raises(ValueError, match="duplicate step name"):
        SyncPipeline([_FakeStep("a"), _FakeStep("a")])


def test_step_names_property_lists_in_registration_order():
    pipeline = SyncPipeline([_FakeStep("a"), _FakeStep("b")])
    assert pipeline.step_names == ["a", "b"]


# ── run input validation ────────────────────────────────────────────


def test_run_rejects_empty_city():
    pipeline = SyncPipeline([_FakeStep("a")])
    with pytest.raises(ValueError, match="city"):
        pipeline.run("")
    with pytest.raises(ValueError):
        pipeline.run("   ")


def test_run_rejects_unknown_only_steps():
    pipeline = SyncPipeline([_FakeStep("google_places"), _FakeStep("practo")])
    with pytest.raises(ValueError, match="unknown step name"):
        pipeline.run("Ludhiana", only_steps=["lybrate"])


# ── happy path ──────────────────────────────────────────────────────


def test_run_invokes_every_step_with_shared_run_id():
    a = _FakeStep("a")
    b = _FakeStep("b")
    pipeline = SyncPipeline([a, b], clock=lambda: _T_NOW)

    result = pipeline.run("Ludhiana", run_id="rid-1")

    assert a.calls == [("Ludhiana", "rid-1")]
    assert b.calls == [("Ludhiana", "rid-1")]
    assert set(result.by_step) == {"a", "b"}
    assert result.run_id == "rid-1"
    assert result.skipped_steps == []


def test_run_aggregates_counts_across_steps():
    a = _FakeStep("a", on_call=lambda c, r: _ok_result("a", c, inserted=3, updated=1))
    b = _FakeStep("b", on_call=lambda c, r: _ok_result("b", c, inserted=7))
    pipeline = SyncPipeline([a, b], clock=lambda: _T_NOW)

    result = pipeline.run("Ludhiana")

    assert result.total_pulled == 11  # 4 + 7
    assert result.total_inserted == 10
    assert result.total_updated == 1
    assert result.any_changes() is True


def test_any_changes_false_when_nothing_pending():
    """Re-run with all rows already synced — no inserts, no updates."""
    a = _FakeStep("a", on_call=lambda c, r: _ok_result("a", c, inserted=0, updated=0))
    pipeline = SyncPipeline([a], clock=lambda: _T_NOW)
    result = pipeline.run("Ludhiana")
    assert result.any_changes() is False


def test_run_generates_run_id_when_not_provided():
    a = _FakeStep("a")
    pipeline = SyncPipeline([a], clock=lambda: _T_NOW)

    result = pipeline.run("Ludhiana")

    assert result.run_id.startswith("sync-")
    assert a.calls[0][1] == result.run_id


# ── only_steps filtering ────────────────────────────────────────────


def test_only_steps_runs_subset_and_records_skipped():
    a = _FakeStep("a")
    b = _FakeStep("b")
    c = _FakeStep("c")
    pipeline = SyncPipeline([a, b, c], clock=lambda: _T_NOW)

    result = pipeline.run("Ludhiana", only_steps=["a", "c"])

    assert a.calls and c.calls
    assert b.calls == []
    assert set(result.by_step) == {"a", "c"}
    assert sorted(result.skipped_steps) == ["b"]


# ── error containment ──────────────────────────────────────────────


def test_step_failure_does_not_abort_other_steps():
    def boom(c, r):
        raise RuntimeError("kaboom")
    a = _FakeStep("a", on_call=boom)
    b = _FakeStep("b", on_call=lambda c, r: _ok_result("b", c, inserted=4))
    pipeline = SyncPipeline([a, b], clock=lambda: _T_NOW)

    result = pipeline.run("Ludhiana")

    assert result.by_step["a"].aborted is True
    assert any("kaboom" in e for e in result.by_step["a"].errors)
    assert result.by_step["b"].inserted == 4
    assert result.any_errors() is True
    assert result.any_aborted() is True


def test_step_can_record_aborted_without_raising():
    aborted = SyncStepResult(
        step_name="a", city="Ludhiana",
        started_at=_T_NOW, finished_at=_T_NOW,
        aborted=True, errors=["drive 503"],
    )
    a = _FakeStep("a", on_call=lambda c, r: aborted)
    b = _FakeStep("b")
    pipeline = SyncPipeline([a, b], clock=lambda: _T_NOW)

    result = pipeline.run("Ludhiana")

    assert result.by_step["a"].aborted is True
    assert b.calls  # b still ran


# ── runtime_checkable Protocol ─────────────────────────────────────


def test_fake_step_satisfies_sync_step_protocol():
    assert isinstance(_FakeStep("x"), SyncStep)
