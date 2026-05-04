"""Lead enrichment pipeline — orchestrates Passes 0–3 + 5 for a city.

Usage:
    pipeline = EnrichLeadsPipeline(...)
    result = pipeline.run("Ludhiana", passes={0, 1, 2, 3, 5})

Each pass is idempotent. Re-running skips leads where the pass is already
recorded in `passes_completed` (unless `force=True`). This means a
partial run (e.g. interrupted halfway through Pass 1) safely resumes.

Pass dependencies:
  Pass 5 (scoring) reads all signals, so it should always run AFTER
  the other passes it depends on. The pipeline enforces this by running
  passes in ascending order regardless of the set passed in.

Rate limiting:
  Pass 2 (website audit) makes one HTTP request per lead. No explicit
  throttling needed — requests are sequential and each takes 1–10s.

  Passes 1 and 2 each make one LLM (Haiku) call per lead. At
  ~$0.001/lead, a 300-lead city run costs ~$0.30 in LLM fees.
"""

from __future__ import annotations

import secrets
import sqlite3
import time

from zelda.db import connect as _db_connect
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from loguru import logger

from zelda.controllers.enrichment import (
    pass0_existing_data,
    pass1_reviews,
    pass2_website,
    pass3_practo,
    pass5_scoring,
)
from zelda.gateways.website_audit import WebsiteAuditGateway
from zelda.models.lead import Lead
from zelda.models.lead_enrichment import LeadEnrichment
from zelda.repositories.google_places_lead_repo import GooglePlacesLeadRepository
from zelda.repositories.lead_enrichment_repo import LeadEnrichmentRepository
from zelda.repositories.lead_repo import LeadRepository
from zelda.repositories.lybrate_listing_repo import LybrateListingRepository
from zelda.repositories.practo_listing_repo import PractoListingRepository
from zelda.repositories.review_repo import ReviewRepository

_ALL_PASSES: frozenset[int] = frozenset({0, 1, 2, 3, 5})
_PASS_NAMES = {0: "pass0", 1: "pass1", 2: "pass2", 3: "pass3", 5: "pass5"}


@dataclass
class EnrichLeadsResult:
    run_id: str
    city: str
    started_at: datetime
    finished_at: datetime | None = None

    n_leads: int = 0
    n_skipped_disqualified: int = 0
    passes_run: dict[int, int] = field(default_factory=dict)
    # pass_number → count of leads where that pass ran (not cached)
    errors: list[str] = field(default_factory=list)

    # score distribution
    n_hot: int = 0
    n_warm: int = 0
    n_cold: int = 0
    n_disqualified: int = 0

    def summary(self) -> str:
        elapsed = ""
        if self.finished_at:
            secs = (self.finished_at - self.started_at).total_seconds()
            elapsed = f" elapsed={secs:.0f}s"
        return (
            f"enrich-leads {self.city}: run_id={self.run_id}"
            f" leads={self.n_leads}"
            f" hot={self.n_hot} warm={self.n_warm}"
            f" cold={self.n_cold} disqualified={self.n_disqualified}"
            f" errors={len(self.errors)}"
            f"{elapsed}"
        )


class EnrichLeadsPipeline:
    def __init__(
        self,
        *,
        db_path: Path | str,
        lead_repo: LeadRepository,
        enrichment_repo: LeadEnrichmentRepository,
        gp_repo: GooglePlacesLeadRepository,
        practo_repo: PractoListingRepository,
        lybrate_repo: LybrateListingRepository,
        review_repo: ReviewRepository,
        anthropic_client: anthropic.Anthropic | None = None,
        website_gateway: WebsiteAuditGateway | None = None,
        inter_lead_delay_s: float = 0.5,
    ) -> None:
        self._db_path = str(db_path)
        self._lead_repo = lead_repo
        self._enrichment_repo = enrichment_repo
        self._gp_repo = gp_repo
        self._practo_repo = practo_repo
        self._lybrate_repo = lybrate_repo
        self._review_repo = review_repo
        self._client = anthropic_client
        self._website_gateway = website_gateway or WebsiteAuditGateway()
        self._inter_lead_delay = inter_lead_delay_s

    def run(
        self,
        city: str,
        *,
        passes: set[int] | None = None,
        force: bool = False,
        run_id: str | None = None,
    ) -> EnrichLeadsResult:
        """Run enrichment for all leads in `city`.

        Args:
            passes: which passes to run (default: all).
            force:  re-run passes even if already completed for a lead.
            run_id: override the auto-generated run identifier.
        """
        enabled = sorted(passes or _ALL_PASSES)
        rid = run_id or _make_run_id()
        now = datetime.now(timezone.utc)
        result = EnrichLeadsResult(run_id=rid, city=city, started_at=now)

        logger.info(
            "enrich-leads.start city={c} run_id={r} passes={p}",
            c=city, r=rid, p=enabled,
        )

        leads = self._lead_repo.get_for_city(city)
        result.n_leads = len(leads)

        if not leads:
            logger.warning("enrich-leads.no_leads city={c}", c=city)
            result.finished_at = datetime.now(timezone.utc)
            return result

        for pass_n in enabled:
            result.passes_run[pass_n] = 0

        # One connection for Pass 3 (raw access to practo_profiles)
        db_conn = _db_connect(self._db_path)
        db_conn.row_factory = sqlite3.Row

        try:
            for i, lead in enumerate(leads, 1):
                logger.info(
                    "enrich-leads.lead {i}/{n} lead_id={lid} name={name}",
                    i=i, n=len(leads), lid=lead.lead_id, name=lead.name,
                )
                enrichment = self._enrichment_repo.get_or_create(
                    lead.lead_id, city=lead.city
                )
                try:
                    enrichment = self._enrich_lead(
                        lead, enrichment, enabled, force, db_conn, result
                    )
                except Exception as e:  # noqa: BLE001
                    msg = f"lead={lead.lead_id} name={lead.name} err={e}"
                    logger.error("enrich-leads.lead_error {msg}", msg=msg)
                    result.errors.append(msg)

                self._enrichment_repo.upsert(enrichment)

                # Tally score tier
                self._tally_tier(enrichment, result)

                if i < len(leads) and self._inter_lead_delay > 0:
                    time.sleep(self._inter_lead_delay)

        finally:
            db_conn.close()

        result.finished_at = datetime.now(timezone.utc)
        logger.info(result.summary())
        return result

    # ── private ────────────────────────────────────────────────────────

    def _enrich_lead(
        self,
        lead: Lead,
        enrichment: LeadEnrichment,
        enabled: list[int],
        force: bool,
        db_conn: sqlite3.Connection,
        result: EnrichLeadsResult,
    ) -> LeadEnrichment:

        for pass_n in enabled:
            pass_name = _PASS_NAMES[pass_n]
            if not force and pass_name in enrichment.passes_completed:
                logger.debug(
                    "enrich-leads.cache_hit lead_id={lid} pass={p}",
                    lid=lead.lead_id, p=pass_name,
                )
                continue

            if pass_n == 0:
                enrichment = pass0_existing_data.run(
                    lead, enrichment,
                    gp_repo=self._gp_repo,
                    practo_repo=self._practo_repo,
                    lybrate_repo=self._lybrate_repo,
                )
            elif pass_n == 1:
                enrichment = pass1_reviews.run(
                    lead, enrichment,
                    review_repo=self._review_repo,
                    anthropic_client=self._client,
                )
            elif pass_n == 2:
                enrichment = pass2_website.run(
                    lead, enrichment,
                    gp_repo=self._gp_repo,
                    gateway=self._website_gateway,
                    anthropic_client=self._client,
                )
            elif pass_n == 3:
                enrichment = pass3_practo.run(
                    lead, enrichment,
                    db_conn=db_conn,
                )
            elif pass_n == 5:
                enrichment = pass5_scoring.run(lead, enrichment)

            result.passes_run[pass_n] = result.passes_run.get(pass_n, 0) + 1

        return enrichment

    @staticmethod
    def _tally_tier(enrichment: LeadEnrichment, result: EnrichLeadsResult) -> None:
        tier = enrichment.score_tier
        if tier == "hot":
            result.n_hot += 1
        elif tier == "warm":
            result.n_warm += 1
        elif tier == "disqualified":
            result.n_disqualified += 1
        else:
            result.n_cold += 1


def _make_run_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"enrich-{ts}-{secrets.token_hex(4)}"


__all__ = ["EnrichLeadsPipeline", "EnrichLeadsResult"]
