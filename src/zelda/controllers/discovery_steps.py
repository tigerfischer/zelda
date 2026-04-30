"""Concrete `DiscoveryStep` implementations.

One class per lead source. Each step is a thin shim that wraps a
source-specific controller (which in turn owns the gateway + repo)
and exposes a uniform `discover_for_city(city, run_id) -> StepResult`
to the `DiscoveryPipeline`.

Per-step knobs (cost limits, page caps, etc.) are configured at
construction time so the pipeline's loop can stay knob-free. The
CLI assembles each step with the right defaults for its source.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from loguru import logger

from zelda.controllers.discover import DiscoverController
from zelda.controllers.discovery_pipeline import StepResult
from zelda.controllers.lybrate_directory import LybrateDirectoryController
from zelda.controllers.practo_directory import PractoDirectoryController


# ── GooglePlacesDiscoveryStep ───────────────────────────────────────


class GooglePlacesDiscoveryStep:
    """Wraps `DiscoverController` (the existing Google Places
    discovery flow) for the pipeline.

    The underlying controller does the heavy lifting: text-search ×
    7 dental queries, dedup by place_id, Place Details fetches for
    new entries, JSONL artifact write, upsert into
    `google_places_leads`. This step adds nothing on top — it just
    translates the controller's `DiscoverResult` into a uniform
    `StepResult`.
    """

    name = "google_places"

    def __init__(
        self,
        controller: DiscoverController,
        *,
        max_results: int | None = 1,
        max_pages_per_query: int = 1,
    ) -> None:
        if max_results is not None and max_results < 0:
            raise ValueError("max_results must be >= 0 or None for unlimited")
        if max_pages_per_query < 1:
            raise ValueError("max_pages_per_query must be >= 1")
        self._controller = controller
        self._max_results = max_results
        self._max_pages_per_query = max_pages_per_query

    def discover_for_city(
        self,
        city: str,
        *,
        run_id: str,
    ) -> StepResult:
        started_at = datetime.now(timezone.utc)
        try:
            inner = self._controller.run(
                city,
                max_results=self._max_results,
                max_pages_per_query=self._max_pages_per_query,
                run_id=run_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "google_places_step.crashed run_id={r} city={c}",
                r=run_id, c=city,
            )
            return StepResult(
                step_name=self.name, city=city,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                aborted=True,
                errors=[f"controller crashed: {type(e).__name__}: {e}"],
            )

        return StepResult(
            step_name=self.name,
            city=city,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            discovered=inner.deduped_total,
            inserted=inner.inserted_count,
            already_known=inner.already_known_count,
            errors=list(inner.errors),
            extras={
                "text_search_total": inner.text_search_total,
                "details_fetched": inner.details_fetched_count,
                "after_max_results": inner.after_max_results_count,
                "artifact_path": (
                    str(inner.artifact_path) if inner.artifact_path else None
                ),
                "max_results": self._max_results,
                "max_pages_per_query": self._max_pages_per_query,
            },
        )


# ── PractoDiscoveryStep ─────────────────────────────────────────────


class PractoDiscoveryStep:
    """Wraps `PractoDirectoryController` for the discovery pipeline.

    Practo's per-city dental-clinic directory is small (~80 entries
    per Indian metro), so unlike Google Places there's no cost knob
    here — the controller always crawls the full directory. The
    gateway's pagination terminates on saturation; the repo's UPSERT
    handles re-runs idempotently.
    """

    name = "practo"

    def __init__(self, controller: PractoDirectoryController) -> None:
        self._controller = controller

    def discover_for_city(
        self,
        city: str,
        *,
        run_id: str,
    ) -> StepResult:
        started_at = datetime.now(timezone.utc)
        try:
            inner = self._controller.run(city, run_id=run_id)
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "practo_step.crashed run_id={r} city={c}", r=run_id, c=city,
            )
            return StepResult(
                step_name=self.name, city=city,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                aborted=True,
                errors=[f"controller crashed: {type(e).__name__}: {e}"],
            )

        return StepResult(
            step_name=self.name,
            city=city,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            discovered=inner.discovered,
            inserted=inner.inserted,
            already_known=inner.already_known,
            errors=list(inner.errors),
        )


# ── LybrateDiscoveryStep ────────────────────────────────────────────


class LybrateDiscoveryStep:
    """Wraps `LybrateDirectoryController` for the discovery pipeline.

    Lybrate's per-city dentist directory is small (~50 entries per
    Indian metro), JSON-LD-clean, and requires no Playwright. Same
    no-cost-knob design as `PractoDiscoveryStep`: the controller
    always crawls the full directory, the gateway terminates on
    saturation, the repo handles re-runs idempotently.
    """

    name = "lybrate"

    def __init__(self, controller: LybrateDirectoryController) -> None:
        self._controller = controller

    def discover_for_city(
        self,
        city: str,
        *,
        run_id: str,
    ) -> StepResult:
        started_at = datetime.now(timezone.utc)
        try:
            inner = self._controller.run(city, run_id=run_id)
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "lybrate_step.crashed run_id={r} city={c}", r=run_id, c=city,
            )
            return StepResult(
                step_name=self.name, city=city,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                aborted=True,
                errors=[f"controller crashed: {type(e).__name__}: {e}"],
            )

        return StepResult(
            step_name=self.name,
            city=city,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            discovered=inner.discovered,
            inserted=inner.inserted,
            already_known=inner.already_known,
            errors=list(inner.errors),
        )


__all__ = [
    "GooglePlacesDiscoveryStep",
    "LybrateDiscoveryStep",
    "PractoDiscoveryStep",
]
