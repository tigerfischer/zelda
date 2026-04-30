"""Practo enrichment controller — orchestrates the gateway + repo to
turn pending stub rows into fully-populated `PractoProfile` rows.

Scope discipline
----------------
This controller deliberately processes ONLY rows that already have a
known Practo URL (i.e. rows in the `practo_profiles` table created via
`repo.upsert_stub`). It does not search Practo or try to discover URLs
from `GooglePlacesLead.website`. URL acquisition is a separate concern; this
controller's sole job is "given a URL, fetch the profile."

Trigger model
-------------
Callable directly (one-shot CLI / smoke script) or by an OS scheduler
for periodic refresh. Idempotent: re-running on a populated DB only
touches stale rows (and pending rows). After every fetch the row is
upserted with the result so partial progress survives a crash.

Failure handling
----------------
- `ok`: persisted as fully enriched.
- `not_found`: persisted as a terminal `not_found` row so we don't
  re-fetch the same dead URL on every run.
- `blocked`: Akamai challenged us. Persisted on the row, AND the loop
  stops — once we're flagged, every subsequent request from the same
  session will hit the same wall.
- `error`: persisted with the exception message; loop continues. The
  next run will retry the row (errors aren't terminal — a transient
  timeout shouldn't quarantine the row forever).
"""

import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Protocol

from loguru import logger

from zelda.gateways.practo_playwright import PractoFetchResult
from zelda.models.practo_profile import PractoProfile
from zelda.repositories.practo_profile_repo import PractoProfileRepository


class _PractoGateway(Protocol):
    """Structural type — what this controller needs from the gateway."""

    def fetch_profile(
        self,
        *,
        place_id: str,
        practo_url: str,
        now: datetime | None = None,
    ) -> PractoFetchResult: ...


@dataclass
class EnrichResult:
    started_at: datetime
    finished_at: datetime | None = None
    n_attempted: int = 0
    n_ok: int = 0
    n_not_found: int = 0
    n_error: int = 0
    n_blocked: int = 0
    stopped_early: bool = False
    """True if the run aborted before processing every candidate
    (typically because Akamai blocked us). The remaining candidates
    stay pending and the next run will pick them up."""
    errors: list[str] = field(default_factory=list)


class EnrichPractoController:
    """Drives Practo enrichment for stub rows.

    Rate-limit posture
    ------------------
    Two dials, both deliberately on the polite side because Practo's
    Akamai watches for traffic patterns AND because we don't want to
    overload Practo even when we're not being throttled:

    - `inter_lead_seconds`: nominal sleep between fetches. Default 4 s.
    - `inter_lead_jitter_seconds`: random extra delay added on top
      (uniform [0, jitter]). Default 3 s — so actual gaps are roughly
      4–7 s between calls.

    For the smoke script and ad-hoc runs, that's ~10 s per lead end-
    to-end (3-ish s navigation + 4–7 s pause) which is conservative
    by design. Set both to 0 in tests for instant runs.

    Lead enrichment is not a deadline-driven workflow — we'd rather
    take an hour to enrich 200 leads than risk getting our IP flagged.
    """

    def __init__(
        self,
        gateway: _PractoGateway,
        repo: PractoProfileRepository,
        *,
        inter_lead_seconds: float = 4.0,
        inter_lead_jitter_seconds: float = 3.0,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        rng: Callable[[float, float], float] | None = None,
    ) -> None:
        if inter_lead_seconds < 0:
            raise ValueError("inter_lead_seconds must be >= 0")
        if inter_lead_jitter_seconds < 0:
            raise ValueError("inter_lead_jitter_seconds must be >= 0")
        self._gateway = gateway
        self._repo = repo
        self._inter_lead_seconds = inter_lead_seconds
        self._inter_lead_jitter_seconds = inter_lead_jitter_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._rng = rng or random.uniform

        if sleep is None:
            import time as _time
            self._sleep = _time.sleep
        else:
            self._sleep = sleep

    # ── public API ─────────────────────────────────────────────────

    def enrich_pending(self, *, max_leads: int | None = None) -> EnrichResult:
        """Process every stub row (`status='pending'`) in the repo.

        Stops early if a rate-limit response is observed — the remaining
        rows stay pending for the next run.
        """
        candidates = self._repo.get_pending(limit=max_leads)
        return self._run(candidates)

    def enrich_one(self, place_id: str) -> PractoProfile | None:
        """Force-refresh one row regardless of current status. Useful
        for the smoke script or a "refresh me one lead" CLI flow."""
        existing = self._repo.get_by_place_id(place_id)
        if existing is None:
            logger.warning("practo.enrich.no_stub place_id={pid}", pid=place_id)
            return None
        self._enrich_row(existing, EnrichResult(started_at=self._clock()))
        return self._repo.get_by_place_id(place_id)

    def enrich_specific(
        self, place_ids: Iterable[str]
    ) -> EnrichResult:
        """Enrich a specific subset of rows by `place_id`. Skips rows
        not present in the repo (with a warning). Useful when the
        operator wants to refresh a curated batch without waiting on
        the staleness window."""
        result = EnrichResult(started_at=self._clock())
        rows: list[PractoProfile] = []
        for pid in place_ids:
            row = self._repo.get_by_place_id(pid)
            if row is None:
                logger.warning("practo.enrich.no_stub place_id={pid}", pid=pid)
                result.errors.append(f"no stub for {pid}")
                continue
            rows.append(row)
        return self._run(rows, result=result)

    # ── internals ─────────────────────────────────────────────────

    def _run(
        self,
        candidates: list[PractoProfile],
        *,
        result: EnrichResult | None = None,
    ) -> EnrichResult:
        result = result or EnrichResult(started_at=self._clock())
        logger.info("practo.enrich.start n={n}", n=len(candidates))

        for i, row in enumerate(candidates):
            stop = self._enrich_row(row, result)
            if stop:
                result.stopped_early = True
                logger.warning(
                    "practo.enrich.stopped_early at={i} of n={n}",
                    i=i + 1, n=len(candidates),
                )
                break
            # Polite pause between calls — skip on the last one.
            if i < len(candidates) - 1:
                base = self._inter_lead_seconds
                jitter = self._inter_lead_jitter_seconds
                if base > 0 or jitter > 0:
                    pause = base + (self._rng(0.0, jitter) if jitter > 0 else 0.0)
                    self._sleep(pause)

        result.finished_at = self._clock()
        logger.info(
            "practo.enrich.done attempted={n} ok={ok} not_found={nf} "
            "blocked={b} error={e} stopped_early={se}",
            n=result.n_attempted, ok=result.n_ok, nf=result.n_not_found,
            b=result.n_blocked, e=result.n_error,
            se=result.stopped_early,
        )
        return result

    def _enrich_row(self, row: PractoProfile, result: EnrichResult) -> bool:
        """Run the gateway on one row and persist the outcome.

        Returns True if the run should stop (Akamai blocked us),
        False to continue.
        """
        result.n_attempted += 1
        fetched_at = self._clock()
        fetch = self._gateway.fetch_profile(
            place_id=row.place_id,
            practo_url=row.practo_url,
            now=fetched_at,
        )

        # Always preserve the original discovered_at from the stub row.
        fetch.profile.discovered_at = row.discovered_at
        self._repo.upsert(fetch.profile, now=fetched_at)

        if fetch.status == "ok":
            result.n_ok += 1
            return False

        if fetch.status == "not_found":
            result.n_not_found += 1
            return False

        if fetch.error_message:
            result.errors.append(fetch.error_message)

        if fetch.status == "blocked":
            result.n_blocked += 1
            return True  # stop the loop — every next call will block too

        # status == 'error'
        result.n_error += 1
        return False
