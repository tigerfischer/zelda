"""Drive-sync pipeline — projects per-source SQLite tables onto Drive
spreadsheets, one sheet per (source, city).

Architecture
------------
- Each source (`google_places`, `practo`, `lybrate`, ...) provides a
  `SyncStep` that knows how to: pull unsynced rows, project them
  onto a sheet-friendly dict, upsert into the source's sheet under
  `{root}/{City}/discovery/{source}`, and stamp `last_synced_at` on
  the persisted rows.
- The `SyncPipeline` runs registered steps in sequence per city.
  Each step's outcome is captured independently — failures don't
  abort siblings.
- The pipeline reads only from SQLite and writes only to Drive. It
  never mutates source-table content other than the
  `last_synced_at` bookkeeping column.

This mirrors the shape of `DiscoveryPipeline` deliberately. Adding
a new source's sync step is a one-class change; the orchestrator
loop is unchanged.

Trigger model
-------------
The pipeline is callable in two modes:
- **One-shot**: `pipeline.run(city)` — flushes pending changes and
  returns. Suitable for CLI use, cron, CI.
- **Watcher**: a long-lived loop that calls `run` every N seconds.
  See `cli.cmd_sync` for the implementation. Decouples Drive from
  any write-path code: discovery/enrichment/matching just bump
  `last_modified_at`; the watcher (a separate process) picks up
  changes on its next tick.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol, runtime_checkable

from loguru import logger


@dataclass
class SyncStepResult:
    """One step's outcome for one city. Each step populates the
    universal counters; source-specific extras (e.g. artifacts
    uploaded for Google Places) go in `extras`."""

    step_name: str
    city: str
    started_at: datetime
    finished_at: datetime | None = None

    pulled: int = 0
    """Rows pulled from the repo as unsynced."""
    inserted: int = 0
    """Rows new in the sheet."""
    updated: int = 0
    """Rows whose sheet representation was overwritten."""
    errors: list[str] = field(default_factory=list)
    aborted: bool = False

    extras: dict[str, object] = field(default_factory=dict)


@dataclass
class SyncPipelineResult:
    """Aggregate outcome for one `SyncPipeline.run` invocation."""

    run_id: str
    city: str
    started_at: datetime
    finished_at: datetime | None = None
    by_step: dict[str, SyncStepResult] = field(default_factory=dict)
    skipped_steps: list[str] = field(default_factory=list)

    @property
    def total_pulled(self) -> int:
        return sum(s.pulled for s in self.by_step.values())

    @property
    def total_inserted(self) -> int:
        return sum(s.inserted for s in self.by_step.values())

    @property
    def total_updated(self) -> int:
        return sum(s.updated for s in self.by_step.values())

    @property
    def step_errors(self) -> list[str]:
        out: list[str] = []
        for s in self.by_step.values():
            out.extend(s.errors)
        return out

    def any_errors(self) -> bool:
        return any(s.errors for s in self.by_step.values())

    def any_aborted(self) -> bool:
        return any(s.aborted for s in self.by_step.values())

    def any_changes(self) -> bool:
        """True if any row was inserted or updated this run."""
        return self.total_inserted > 0 or self.total_updated > 0


@runtime_checkable
class SyncStep(Protocol):
    """One source-specific sync step.

    Implementations MUST:
    - Read only from the source's SQLite table (via its repo's
      `get_unsynced_for_city` / `mark_synced`).
    - Write only to the source's sheet under
      `{root}/{City}/discovery/{source}`.
    - Be a no-op when the source has no unsynced rows for the city.
    - Catch their own errors and report via `SyncStepResult.errors`.
      Reserved-for-catastrophic raising is allowed but the pipeline
      will still record the step as aborted.
    """

    @property
    def name(self) -> str: ...

    def run_for_city(
        self,
        city: str,
        *,
        run_id: str,
    ) -> SyncStepResult: ...


class SyncPipeline:
    """Runs a list of `SyncStep` instances for one city. Same shape
    as `DiscoveryPipeline`."""

    def __init__(
        self,
        steps: list[SyncStep],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not steps:
            raise ValueError("at least one SyncStep is required")
        seen: set[str] = set()
        for s in steps:
            if s.name in seen:
                raise ValueError(f"duplicate step name: {s.name!r}")
            seen.add(s.name)
        self._steps = steps
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def step_names(self) -> list[str]:
        return [s.name for s in self._steps]

    def run(
        self,
        city: str,
        *,
        only_steps: list[str] | None = None,
        run_id: str | None = None,
    ) -> SyncPipelineResult:
        if not city or not city.strip():
            raise ValueError("city must be non-empty")

        rid = run_id or _make_run_id()
        result = SyncPipelineResult(
            run_id=rid, city=city, started_at=self._clock(),
        )

        if only_steps is not None:
            requested = set(only_steps)
            unknown = requested - set(self.step_names)
            if unknown:
                raise ValueError(
                    f"unknown step name(s): {sorted(unknown)}; "
                    f"available: {self.step_names}"
                )
            steps_to_run = [s for s in self._steps if s.name in requested]
            result.skipped_steps = [
                s.name for s in self._steps if s.name not in requested
            ]
        else:
            steps_to_run = list(self._steps)

        logger.info(
            "sync.start run_id={r} city={c} steps={s} skipped={sk}",
            r=rid, c=city,
            s=[s.name for s in steps_to_run],
            sk=result.skipped_steps,
        )

        for step in steps_to_run:
            result.by_step[step.name] = self._run_one_step(step, city, rid)

        result.finished_at = self._clock()
        logger.info(
            "sync.done run_id={r} city={c} steps_ran={n} pulled={p} "
            "inserted={i} updated={u} errors={e} aborted={a}",
            r=rid, c=city, n=len(result.by_step),
            p=result.total_pulled, i=result.total_inserted,
            u=result.total_updated,
            e=len(result.step_errors), a=result.any_aborted(),
        )
        return result

    def _run_one_step(
        self,
        step: SyncStep,
        city: str,
        run_id: str,
    ) -> SyncStepResult:
        try:
            return step.run_for_city(city, run_id=run_id)
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "sync.step_crashed run_id={r} step={s} city={c}",
                r=run_id, s=step.name, c=city,
            )
            now = datetime.now(timezone.utc)
            return SyncStepResult(
                step_name=step.name,
                city=city,
                started_at=now,
                finished_at=now,
                aborted=True,
                errors=[f"step crashed: {type(e).__name__}: {e}"],
            )


def _make_run_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"sync-{ts}-{secrets.token_hex(4)}"


__all__ = [
    "SyncPipeline",
    "SyncPipelineResult",
    "SyncStep",
    "SyncStepResult",
]
