"""Tests for concrete `DiscoveryStep` implementations.

GooglePlacesDiscoveryStep is the only one tested here today; new
steps land their tests alongside the step class. The underlying
controllers have their own unit tests; these tests verify only the
step's translation layer (controller call args + `StepResult`
mapping + error containment).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from datetime import datetime, timezone

from zelda.controllers.discover import DiscoverResult
from zelda.controllers.discovery_pipeline import DiscoveryStep
from zelda.controllers.discovery_steps import (
    GooglePlacesDiscoveryStep,
    LybrateDiscoveryStep,
    PractoDiscoveryStep,
)
from zelda.controllers.lybrate_directory import LybrateDirectoryResult
from zelda.controllers.practo_directory import PractoDirectoryResult


_T_NOW = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)


# ── construction validation ─────────────────────────────────────────


def test_max_results_must_be_non_negative_or_none():
    ctrl = MagicMock()
    with pytest.raises(ValueError, match="max_results"):
        GooglePlacesDiscoveryStep(ctrl, max_results=-1)
    # Both None and 0 are valid (None = unlimited, 0 = dry-run).
    GooglePlacesDiscoveryStep(ctrl, max_results=None)
    GooglePlacesDiscoveryStep(ctrl, max_results=0)


def test_max_pages_per_query_must_be_positive():
    ctrl = MagicMock()
    with pytest.raises(ValueError, match="max_pages_per_query"):
        GooglePlacesDiscoveryStep(ctrl, max_pages_per_query=0)


# ── happy path ──────────────────────────────────────────────────────


def test_step_forwards_knobs_to_controller():
    ctrl = MagicMock()
    ctrl.run.return_value = DiscoverResult(run_id="rid-1", city="Ludhiana")
    step = GooglePlacesDiscoveryStep(
        ctrl, max_results=5, max_pages_per_query=3,
    )

    step.discover_for_city("Ludhiana", run_id="rid-1")

    ctrl.run.assert_called_once_with(
        "Ludhiana",
        max_results=5,
        max_pages_per_query=3,
        run_id="rid-1",
    )


def test_step_translates_controller_result_to_step_result():
    inner = DiscoverResult(
        run_id="rid-1", city="Ludhiana",
        text_search_total=120,
        deduped_total=60,
        already_known_count=2,
        new_eligible_count=58,
        after_max_results_count=5,
        details_fetched_count=5,
        inserted_count=5,
        errors=["one warning"],
        artifact_path=Path("/tmp/x.jsonl"),
    )
    ctrl = MagicMock()
    ctrl.run.return_value = inner
    step = GooglePlacesDiscoveryStep(ctrl, max_results=5)

    result = step.discover_for_city("Ludhiana", run_id="rid-1")

    assert result.step_name == "google_places"
    assert result.city == "Ludhiana"
    assert result.discovered == 60
    assert result.inserted == 5
    assert result.already_known == 2
    assert result.errors == ["one warning"]
    assert result.aborted is False
    assert result.extras["text_search_total"] == 120
    assert result.extras["details_fetched"] == 5
    assert result.extras["after_max_results"] == 5
    assert result.extras["artifact_path"] == "/tmp/x.jsonl"
    assert result.extras["max_results"] == 5


def test_step_handles_no_artifact_path():
    """artifact_path is None when nothing was fetched (max_results=0)."""
    inner = DiscoverResult(
        run_id="rid-1", city="Ludhiana",
        deduped_total=60, after_max_results_count=0,
    )
    ctrl = MagicMock(); ctrl.run.return_value = inner
    step = GooglePlacesDiscoveryStep(ctrl, max_results=0)

    result = step.discover_for_city("Ludhiana", run_id="rid-1")

    assert result.extras["artifact_path"] is None


# ── error containment ──────────────────────────────────────────────


def test_step_catches_controller_exception():
    ctrl = MagicMock()
    ctrl.run.side_effect = RuntimeError("network down")
    step = GooglePlacesDiscoveryStep(ctrl)

    result = step.discover_for_city("Ludhiana", run_id="rid-1")

    assert result.aborted is True
    assert any("network down" in e for e in result.errors)
    assert result.discovered == 0
    assert result.inserted == 0


# ── Protocol conformance ───────────────────────────────────────────


def test_google_places_step_satisfies_discovery_step_protocol():
    step = GooglePlacesDiscoveryStep(MagicMock())
    assert isinstance(step, DiscoveryStep)


# ── PractoDiscoveryStep ─────────────────────────────────────────────


def _practo_inner(
    *, discovered: int = 5, inserted: int = 5, already_known: int = 0,
    errors: list[str] | None = None,
) -> PractoDirectoryResult:
    return PractoDirectoryResult(
        city="Ludhiana", run_id="rid-1", started_at=_T_NOW,
        finished_at=_T_NOW,
        discovered=discovered, inserted=inserted, already_known=already_known,
        errors=errors or [],
    )


def test_practo_step_translates_controller_result_to_step_result():
    ctrl = MagicMock()
    ctrl.run.return_value = _practo_inner(
        discovered=82, inserted=80, already_known=2, errors=["one warning"],
    )
    step = PractoDiscoveryStep(ctrl)

    result = step.discover_for_city("Ludhiana", run_id="rid-1")

    ctrl.run.assert_called_once_with("Ludhiana", run_id="rid-1")
    assert result.step_name == "practo"
    assert result.city == "Ludhiana"
    assert result.discovered == 82
    assert result.inserted == 80
    assert result.already_known == 2
    assert result.errors == ["one warning"]
    assert result.aborted is False


def test_practo_step_catches_controller_exception():
    ctrl = MagicMock()
    ctrl.run.side_effect = RuntimeError("boom")
    step = PractoDiscoveryStep(ctrl)

    result = step.discover_for_city("Ludhiana", run_id="rid-1")

    assert result.aborted is True
    assert any("boom" in e for e in result.errors)
    assert result.discovered == 0


def test_practo_step_satisfies_discovery_step_protocol():
    assert isinstance(PractoDiscoveryStep(MagicMock()), DiscoveryStep)


# ── LybrateDiscoveryStep ────────────────────────────────────────────


def _lybrate_inner(
    *, discovered: int = 5, inserted: int = 5, already_known: int = 0,
    errors: list[str] | None = None,
) -> LybrateDirectoryResult:
    return LybrateDirectoryResult(
        city="Ludhiana", run_id="rid-1", started_at=_T_NOW,
        finished_at=_T_NOW,
        discovered=discovered, inserted=inserted, already_known=already_known,
        errors=errors or [],
    )


def test_lybrate_step_translates_controller_result_to_step_result():
    ctrl = MagicMock()
    ctrl.run.return_value = _lybrate_inner(
        discovered=50, inserted=48, already_known=2, errors=["one warning"],
    )
    step = LybrateDiscoveryStep(ctrl)

    result = step.discover_for_city("Ludhiana", run_id="rid-1")

    ctrl.run.assert_called_once_with("Ludhiana", run_id="rid-1")
    assert result.step_name == "lybrate"
    assert result.city == "Ludhiana"
    assert result.discovered == 50
    assert result.inserted == 48
    assert result.already_known == 2
    assert result.errors == ["one warning"]
    assert result.aborted is False


def test_lybrate_step_catches_controller_exception():
    ctrl = MagicMock()
    ctrl.run.side_effect = RuntimeError("network down")
    step = LybrateDiscoveryStep(ctrl)

    result = step.discover_for_city("Ludhiana", run_id="rid-1")

    assert result.aborted is True
    assert any("network down" in e for e in result.errors)


def test_lybrate_step_satisfies_discovery_step_protocol():
    assert isinstance(LybrateDiscoveryStep(MagicMock()), DiscoveryStep)
