"""Discovery pipeline — runs N parallel-conceptually source steps for
a given city.

Architecture
------------
- Each lead source (Google Places, Practo, Lybrate, IDA, ...) is a
  `DiscoveryStep`. A step owns its gateway/repo/controller chain and
  knows nothing about other steps.
- The `DiscoveryPipeline` accepts a list of steps, runs each for a
  given city, and reports per-step + aggregate stats.
- Steps are independent. A failure or block in one step does NOT
  abort the others — each step's outcome is captured in its own
  `StepResult` and logged. The pipeline returns successfully even if
  every step errored; the caller (CLI) decides exit policy.
- Today the pipeline runs steps sequentially. The shape leaves room
  for true concurrency (each step writes to its own table, so there
  are no in-memory contention points), but until we have evidence
  that latency matters, sequential is simpler and gives identical
  semantics.

Adding a new source
-------------------
1. Build a per-source gateway (network) + repo (persistence).
2. Optionally build a per-source controller if there's enough
   per-city orchestration logic to be worth a layer (e.g. JSONL
   artifact writing). Otherwise, the step itself can directly own
   the gateway+repo.
3. Implement `DiscoveryStep` as a thin shim — give it a unique
   `name` and a `discover_for_city` method that returns a
   `StepResult`.
4. Register the step in the CLI's pipeline construction.

The orchestrator's loop logic doesn't change.

Why a Protocol rather than an abstract base class?
--------------------------------------------------
Same rationale as `enrichment_sources.SourceAdapter`: each source
has its own controller layer with source-specific knobs. A
structural type lets each `DiscoveryStep` be the thinnest possible
bridge between the pipeline and its source-specific machinery.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol, runtime_checkable

from loguru import logger


@dataclass
class StepResult:
    """One step's outcome for one city. Kept deliberately small —
    each step can attach source-specific extras via `extras`."""

    step_name: str
    city: str
    started_at: datetime
    finished_at: datetime | None = None

    # Universal counters every step should populate:
    discovered: int = 0          # total entries the source returned
    inserted: int = 0            # rows newly written to the source's table
    updated: int = 0             # rows whose mutable fields changed on upsert
    already_known: int = 0       # rows already in DB before this run
    errors: list[str] = field(default_factory=list)
    aborted: bool = False        # set if the step gave up partway

    # Anything else (cost knobs, page counts, payload sizes) goes here.
    extras: dict[str, object] = field(default_factory=dict)


@dataclass
class PipelineResult:
    """Aggregate outcome for one `DiscoveryPipeline.run` invocation."""

    run_id: str
    city: str
    started_at: datetime
    finished_at: datetime | None = None
    by_step: dict[str, StepResult] = field(default_factory=dict)
    skipped_steps: list[str] = field(default_factory=list)
    """Steps the caller filtered out via `only_steps`."""

    @property
    def total_inserted(self) -> int:
        return sum(s.inserted for s in self.by_step.values())

    @property
    def total_discovered(self) -> int:
        return sum(s.discovered for s in self.by_step.values())

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


@runtime_checkable
class DiscoveryStep(Protocol):
    """One source-specific discovery step.

    Implementations MUST:
    - Be idempotent across runs for the same city. Re-running on a
      city the source has already covered should be a no-op (or a
      thin update if the source's data changed).
    - Persist their results to their OWN per-source table — never to
      a shared "leads" table. Cross-source linking is handled by a
      future matching phase.
    - Catch their own errors and surface them via `StepResult.errors`.
      Raising from `discover_for_city` is reserved for catastrophic
      problems (e.g., misconfiguration); routine source-side failures
      should be reported in the result.
    - Set `aborted=True` if the step gave up partway (e.g. CAPTCHA /
      rate-limit) so the pipeline can log it and the caller can
      decide whether to retry later.
    """

    @property
    def name(self) -> str: ...

    def discover_for_city(
        self,
        city: str,
        *,
        run_id: str,
    ) -> StepResult: ...


class DiscoveryPipeline:
    """Runs a list of `DiscoveryStep` instances for one city.

    Steps execute sequentially today. Each step writes to its own
    per-source table; there is no cross-step state.
    """

    def __init__(
        self,
        steps: list[DiscoveryStep],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not steps:
            raise ValueError("at least one DiscoveryStep is required")
        seen_names: set[str] = set()
        for s in steps:
            if s.name in seen_names:
                raise ValueError(f"duplicate step name: {s.name!r}")
            seen_names.add(s.name)
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
    ) -> PipelineResult:
        """Run every registered step for `city`. If `only_steps` is
        supplied, only the named subset runs; the rest are listed in
        `skipped_steps`. `run_id` is shared across all steps so a
        single discovery invocation produces correlated logs."""
        if not city or not city.strip():
            raise ValueError("city must be non-empty")

        rid = run_id or _make_run_id()
        result = PipelineResult(
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
            "discovery.start run_id={r} city={c} steps={s} "
            "skipped={sk}",
            r=rid, c=city,
            s=[s.name for s in steps_to_run],
            sk=result.skipped_steps,
        )

        for step in steps_to_run:
            step_result = self._run_one_step(step, city, rid)
            result.by_step[step.name] = step_result

        result.finished_at = self._clock()
        logger.info(
            "discovery.done run_id={r} city={c} steps_ran={n} "
            "discovered={d} inserted={i} errors={e} aborted={a}",
            r=rid, c=city, n=len(result.by_step),
            d=result.total_discovered, i=result.total_inserted,
            e=len(result.step_errors), a=result.any_aborted(),
        )
        return result

    def _run_one_step(
        self,
        step: DiscoveryStep,
        city: str,
        run_id: str,
    ) -> StepResult:
        """Run one step with full error containment. A crashing step
        becomes a step-level error, not a pipeline failure."""
        try:
            return step.discover_for_city(city, run_id=run_id)
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "discovery.step_crashed run_id={r} step={s} city={c}",
                r=run_id, s=step.name, c=city,
            )
            now = datetime.now(timezone.utc)
            return StepResult(
                step_name=step.name,
                city=city,
                started_at=now,
                finished_at=now,
                aborted=True,
                errors=[f"step crashed: {type(e).__name__}: {e}"],
            )


def _make_run_id() -> str:
    """ISO-ish + 4 hex bytes — small enough for log lines, unique
    enough to correlate across log streams."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{ts}-{secrets.token_hex(4)}"


__all__ = [
    "DiscoveryPipeline",
    "DiscoveryStep",
    "PipelineResult",
    "StepResult",
]
