"""Enrichment orchestrator — coordinates multiple `SourceAdapter`s
across all leads in a city, with source-level caching.

Per (lead × source), the orchestrator decides one of four actions:

1. **Skip — no prerequisite data**: `source.can_fetch(lead) == False`.
   For example, the Practo adapter requires a known practo_url stub
   row; if one doesn't exist, Practo gets skipped for that lead with
   no error. The orchestrator records this distinctly from a fetch
   failure.

2. **Skip — cache hit**: `source.is_cached_fresh(...) == True`. We
   already have recent successful data for this (lead, source) pair.
   No fetch, no I/O.

3. **Fetch and record success**: `source.fetch_for_lead(...)` returned
   a status in SUCCESSFUL_STATUSES.

4. **Fetch and record failure**: status in ERROR_STATUSES (continue
   the run) OR BLOCKED_STATUSES (mark this source as blocked-for-the-
   rest-of-this-run; the source isn't re-attempted but other sources
   continue).

Per-source blocking is intentional: if Google blocks our reviews
scraper, that's an environmental issue specific to the reviews
gateway. Practo (running on a different domain, different browser
session) is unaffected. So we burn the blocked source for the rest
of the run, not the whole run.

Rate-limit posture
------------------
The orchestrator owns inter-lead pacing because it's bypassing
each controller's own city-loop pacing. Default 8–20 s random
between leads — same posture as `FetchReviewsController.run()`.
Per-source delays inside `fetch_for_lead` (gateway-internal scroll
sleeps, post-navigation settles) still apply — the orchestrator's
delay is on top.
"""

import random
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from loguru import logger

from zelda.controllers.enrichment_sources import (
    BLOCKED_STATUSES,
    ERROR_STATUSES,
    SKIPPED_STATUSES,
    SUCCESSFUL_STATUSES,
    SourceAdapter,
)
from zelda.models.raw_lead import RawLead
from zelda.repositories.raw_lead_repo import RawLeadRepository


@dataclass
class SourceStats:
    """Per-source stats accumulated over one orchestrator run."""

    n_cache_hits: int = 0
    """Skipped because is_cached_fresh was True."""
    n_no_prereq: int = 0
    """Skipped because can_fetch was False (e.g. no Practo URL)."""
    n_skipped_blocked_earlier: int = 0
    """Skipped because this source got blocked earlier in this run."""
    n_attempted: int = 0
    """fetch_for_lead actually called."""
    n_successful: int = 0
    n_errored: int = 0
    n_blocked: int = 0
    n_other_terminal: int = 0
    """e.g. Practo's "not_found" status — terminal but not a failure."""


@dataclass
class OrchestratorResult:
    city: str
    run_id: str
    started_at: datetime
    finished_at: datetime | None = None

    n_leads_in_city: int = 0
    n_after_max_leads: int = 0
    by_source: dict[str, SourceStats] = field(default_factory=dict)
    """Per-source aggregate stats. Keys are source names ("google_reviews",
    "practo_profile", ...)."""
    captures: list[dict[str, Any]] = field(default_factory=list)
    """Per-(lead × source) summary dicts as returned by adapters'
    fetch_for_lead. Includes a `source` field on every record."""
    errors: list[str] = field(default_factory=list)
    blocked_sources: list[str] = field(default_factory=list)
    """Sources that hit a block at any point in this run, in order
    they tripped. Useful for surfacing in the CLI summary."""


class EnrichmentOrchestrator:
    def __init__(
        self,
        sources: list[SourceAdapter],
        lead_repo: RawLeadRepository,
        *,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        inter_lead_delay_range: tuple[float, float] = (8.0, 20.0),
    ) -> None:
        if not sources:
            raise ValueError("sources must be non-empty")
        # Disallow duplicate source names — would corrupt by_source dict.
        names = [s.name for s in sources]
        if len(set(names)) != len(names):
            raise ValueError(
                f"duplicate source names: {names}"
            )
        self._sources = sources
        self._lead_repo = lead_repo
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleeper = sleeper or time.sleep
        self._inter_lead_delay_range = inter_lead_delay_range

    def enrich_city(
        self,
        city: str,
        *,
        only_sources: list[str] | None = None,
        max_leads: int | None = None,
        max_age_days: float = 180.0,
        force_refresh: bool = False,
        run_id: str | None = None,
    ) -> OrchestratorResult:
        """Iterate over leads × sources for `city`.

        - `only_sources` filters which registered sources run this pass.
          Default = all.
        - `max_leads` caps how many leads this run touches (cost knob).
        - `max_age_days` is the source-level cache window. 180 (6 mo)
          by default per project decision — fine for stable Practo data
          and acceptable for reviews (steady-state new reviews per
          clinic are slow).
        - `force_refresh` bypasses is_cached_fresh entirely.
        """
        if not city or not city.strip():
            raise ValueError("city must be non-empty")
        if max_leads is not None and max_leads < 0:
            raise ValueError("max_leads must be >= 0 (or None for unlimited)")
        if max_age_days < 0:
            raise ValueError("max_age_days must be >= 0")

        started_at = self._clock()
        run_id = run_id or _make_run_id(started_at)

        active_sources = self._select_sources(only_sources)
        result = OrchestratorResult(
            city=city,
            run_id=run_id,
            started_at=started_at,
            by_source={s.name: SourceStats() for s in active_sources},
        )

        logger.info(
            "enrich.start city={city} run_id={run_id} sources={ns} "
            "max_leads={ml} max_age_days={mad} force_refresh={fr}",
            city=city, run_id=run_id,
            ns=[s.name for s in active_sources],
            ml=max_leads, mad=max_age_days, fr=force_refresh,
        )

        leads = self._lead_repo.get_for_city(city)
        result.n_leads_in_city = len(leads)
        if max_leads is not None:
            leads = leads[:max_leads]
        result.n_after_max_leads = len(leads)

        if not leads:
            result.finished_at = self._clock()
            logger.info("enrich.noop city={city} reason=no_leads", city=city)
            return result

        # Per-source blocked-for-this-run flag. Once a source trips,
        # it stays disabled for remaining leads but other sources keep going.
        blocked_set: set[str] = set()

        for i, lead in enumerate(leads, start=1):
            if i > 1:
                self._inter_lead_sleep()

            for source in active_sources:
                self._process_one(
                    lead=lead,
                    source=source,
                    blocked_set=blocked_set,
                    capture_id=f"{run_id}-{i:04d}-{source.name}",
                    now=self._clock(),
                    max_age_days=max_age_days,
                    force_refresh=force_refresh,
                    result=result,
                )

        result.finished_at = self._clock()
        logger.info(
            "enrich.done city={city} run_id={run_id} leads={n} "
            "blocked_sources={bs}",
            city=city, run_id=run_id,
            n=result.n_after_max_leads,
            bs=result.blocked_sources,
        )
        return result

    # ── internals ────────────────────────────────────────────────────

    def _select_sources(
        self, only_sources: list[str] | None,
    ) -> list[SourceAdapter]:
        if only_sources is None:
            return list(self._sources)
        wanted = set(only_sources)
        unknown = wanted - {s.name for s in self._sources}
        if unknown:
            raise ValueError(
                f"unknown source name(s): {sorted(unknown)}; "
                f"registered: {sorted(s.name for s in self._sources)}"
            )
        return [s for s in self._sources if s.name in wanted]

    def _process_one(
        self,
        *,
        lead: RawLead,
        source: SourceAdapter,
        blocked_set: set[str],
        capture_id: str,
        now: datetime,
        max_age_days: float,
        force_refresh: bool,
        result: OrchestratorResult,
    ) -> None:
        stats = result.by_source[source.name]

        if source.name in blocked_set:
            stats.n_skipped_blocked_earlier += 1
            return

        if not source.can_fetch(lead):
            stats.n_no_prereq += 1
            return

        if not force_refresh and source.is_cached_fresh(
            lead.place_id, max_age_days=max_age_days, now=now,
        ):
            stats.n_cache_hits += 1
            return

        # Actually fetch
        try:
            summary = source.fetch_for_lead(
                lead, capture_id=capture_id, now=now,
            )
        except Exception as e:  # noqa: BLE001
            # Adapter contract says MUST NOT raise — but defend anyway.
            msg = (
                f"adapter {source.name} raised for place_id={lead.place_id}: "
                f"{type(e).__name__}: {e}"
            )
            logger.error(msg)
            result.errors.append(msg)
            stats.n_attempted += 1
            stats.n_errored += 1
            return

        stats.n_attempted += 1
        result.captures.append(summary)
        status = summary.get("fetch_status", "error")

        if status in SUCCESSFUL_STATUSES:
            stats.n_successful += 1
        elif status in BLOCKED_STATUSES:
            stats.n_blocked += 1
            blocked_set.add(source.name)
            result.blocked_sources.append(source.name)
            logger.error(
                "enrich.source_blocked source={src} after_lead={pid} — "
                "skipping this source for remaining leads",
                src=source.name, pid=lead.place_id,
            )
        elif status in ERROR_STATUSES:
            stats.n_errored += 1
            err = summary.get("error_message")
            if err:
                result.errors.append(f"[{source.name}] {err}")
        elif status in SKIPPED_STATUSES:
            stats.n_other_terminal += 1
        else:
            # Unknown status — log + count as error
            logger.warning(
                "enrich.unknown_status source={src} status={st} place_id={pid}",
                src=source.name, st=status, pid=lead.place_id,
            )
            stats.n_errored += 1

    def _inter_lead_sleep(self) -> None:
        low, high = self._inter_lead_delay_range
        if high <= low:
            self._sleeper(low)
            return
        self._sleeper(random.uniform(low, high))


def _make_run_id(now: datetime) -> str:
    """Sortable run id: YYYYMMDD-HHMMSS-XXXX."""
    ts = now.strftime("%Y%m%d-%H%M%S")
    suffix = secrets.token_hex(2)
    return f"{ts}-{suffix}"
