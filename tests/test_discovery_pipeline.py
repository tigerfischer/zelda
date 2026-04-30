"""Tests for `DiscoveryPipeline` — the orchestrator that runs N
parallel-conceptually source steps for one city.

We use a tiny `_FakeStep` here rather than wiring up a real source
gateway. The pipeline's job is the loop logic + error containment +
result aggregation; each real source has its own dedicated tests.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

import pytest

from zelda.controllers.discovery_pipeline import (
    DiscoveryPipeline,
    DiscoveryStep,
    PipelineResult,
    StepResult,
)


_T_NOW = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)


class _FakeStep:
    """Test double for `DiscoveryStep`. Records every call and lets
    the test script the outcome (returning a result, raising)."""

    def __init__(
        self,
        name: str,
        *,
        on_call: Callable[[str, str], StepResult] | None = None,
    ) -> None:
        self._name = name
        self._on_call = on_call
        self.calls: list[tuple[str, str]] = []

    @property
    def name(self) -> str:
        return self._name

    def discover_for_city(self, city: str, *, run_id: str) -> StepResult:
        self.calls.append((city, run_id))
        if self._on_call is not None:
            return self._on_call(city, run_id)
        return StepResult(
            step_name=self._name, city=city,
            started_at=_T_NOW, finished_at=_T_NOW,
            discovered=10, inserted=8,
        )


def _ok_result(name: str, city: str, *, inserted: int = 5) -> StepResult:
    return StepResult(
        step_name=name, city=city,
        started_at=_T_NOW, finished_at=_T_NOW,
        discovered=inserted, inserted=inserted,
    )


# ── construction validation ─────────────────────────────────────────


def test_pipeline_requires_at_least_one_step():
    with pytest.raises(ValueError, match="at least one"):
        DiscoveryPipeline([])


def test_pipeline_rejects_duplicate_step_names():
    with pytest.raises(ValueError, match="duplicate step name"):
        DiscoveryPipeline([_FakeStep("a"), _FakeStep("a")])


def test_step_names_property_lists_in_registration_order():
    pipeline = DiscoveryPipeline([
        _FakeStep("a"), _FakeStep("b"), _FakeStep("c"),
    ])
    assert pipeline.step_names == ["a", "b", "c"]


# ── run input validation ────────────────────────────────────────────


def test_run_rejects_empty_city():
    pipeline = DiscoveryPipeline([_FakeStep("a")])
    with pytest.raises(ValueError, match="city must be non-empty"):
        pipeline.run("")
    with pytest.raises(ValueError):
        pipeline.run("   ")


def test_run_rejects_unknown_only_steps():
    pipeline = DiscoveryPipeline([_FakeStep("google_places"), _FakeStep("practo")])
    with pytest.raises(ValueError, match="unknown step name"):
        pipeline.run("Ludhiana", only_steps=["lybrate"])


# ── happy path ──────────────────────────────────────────────────────


def test_run_invokes_every_step_with_shared_run_id():
    a = _FakeStep("a")
    b = _FakeStep("b")
    pipeline = DiscoveryPipeline([a, b], clock=lambda: _T_NOW)

    result = pipeline.run("Ludhiana", run_id="rid-1")

    assert a.calls == [("Ludhiana", "rid-1")]
    assert b.calls == [("Ludhiana", "rid-1")]
    assert set(result.by_step) == {"a", "b"}
    assert result.run_id == "rid-1"
    assert result.skipped_steps == []


def test_run_aggregates_counts_across_steps():
    a = _FakeStep("a", on_call=lambda c, r: _ok_result("a", c, inserted=3))
    b = _FakeStep("b", on_call=lambda c, r: _ok_result("b", c, inserted=7))
    pipeline = DiscoveryPipeline([a, b], clock=lambda: _T_NOW)

    result = pipeline.run("Ludhiana")

    assert result.total_inserted == 10
    assert result.total_discovered == 10


def test_run_generates_run_id_when_not_provided():
    a = _FakeStep("a")
    pipeline = DiscoveryPipeline([a], clock=lambda: _T_NOW)

    result = pipeline.run("Ludhiana")

    assert result.run_id  # non-empty
    # Sanity: the format includes a timestamp-ish prefix and hex suffix
    assert "-" in result.run_id
    # And it propagated to the step
    assert a.calls[0][1] == result.run_id


# ── only_steps filtering ────────────────────────────────────────────


def test_only_steps_runs_subset_and_records_skipped():
    a = _FakeStep("a")
    b = _FakeStep("b")
    c = _FakeStep("c")
    pipeline = DiscoveryPipeline([a, b, c], clock=lambda: _T_NOW)

    result = pipeline.run("Ludhiana", only_steps=["a", "c"])

    assert a.calls and c.calls
    assert b.calls == []  # skipped — step.discover_for_city never called
    assert set(result.by_step) == {"a", "c"}
    assert sorted(result.skipped_steps) == ["b"]


# ── error containment ──────────────────────────────────────────────


def test_step_failure_does_not_abort_other_steps():
    """A crashing step becomes a step-level error; siblings still run."""
    def boom(c, r):
        raise RuntimeError("kaboom")
    a = _FakeStep("a", on_call=boom)
    b = _FakeStep("b", on_call=lambda c, r: _ok_result("b", c, inserted=4))
    pipeline = DiscoveryPipeline([a, b], clock=lambda: _T_NOW)

    result = pipeline.run("Ludhiana")

    # a's result is recorded as aborted with the error captured.
    assert "a" in result.by_step
    a_result = result.by_step["a"]
    assert a_result.aborted is True
    assert any("kaboom" in e for e in a_result.errors)

    # b ran normally.
    assert result.by_step["b"].inserted == 4
    assert b.calls

    # Aggregates reflect the mix.
    assert result.any_errors() is True
    assert result.any_aborted() is True
    assert result.total_inserted == 4


def test_step_can_record_aborted_without_raising():
    """A step that decides to give up (CAPTCHA, rate limit) sets
    aborted=True itself — pipeline reports that without re-wrapping."""
    aborted_result = StepResult(
        step_name="a", city="Ludhiana",
        started_at=_T_NOW, finished_at=_T_NOW,
        aborted=True, errors=["akamai blocked us"],
    )
    a = _FakeStep("a", on_call=lambda c, r: aborted_result)
    b = _FakeStep("b")
    pipeline = DiscoveryPipeline([a, b], clock=lambda: _T_NOW)

    result = pipeline.run("Ludhiana")

    assert result.by_step["a"].aborted is True
    assert result.any_aborted() is True
    # b still ran
    assert b.calls


# ── PipelineResult API ──────────────────────────────────────────────


def test_pipeline_result_aggregates_step_errors():
    a_result = StepResult(
        step_name="a", city="Ludhiana",
        started_at=_T_NOW, finished_at=_T_NOW,
        errors=["err1", "err2"],
    )
    b_result = StepResult(
        step_name="b", city="Ludhiana",
        started_at=_T_NOW, finished_at=_T_NOW,
        errors=["err3"],
    )
    a = _FakeStep("a", on_call=lambda c, r: a_result)
    b = _FakeStep("b", on_call=lambda c, r: b_result)
    pipeline = DiscoveryPipeline([a, b], clock=lambda: _T_NOW)

    result = pipeline.run("Ludhiana")

    assert sorted(result.step_errors) == ["err1", "err2", "err3"]


# ── runtime_checkable Protocol ─────────────────────────────────────


def test_fake_step_satisfies_discovery_step_protocol():
    assert isinstance(_FakeStep("x"), DiscoveryStep)
