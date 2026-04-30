"""Matching pipeline — orchestrates all five stages for one city.

Stage sequence:
  1. Load rows from all three source repos for the city.
  2. Project to MatchableRow (unified interface).
  3. Pre-filter → candidate pairs for each of the three source pairs.
  4. LLM Judge (Proposer + Reviewer) per candidate pair.
  5. Build match graph; flag conflicts.
  6. Synthesis LLM → one Lead per cluster.
  7. Standalone rows → one Lead each.
  8. Persist all leads to LeadRepository.

Source pairs evaluated (three unique pairs, both directions covered by the
match graph):
  - google_places ↔ practo    (geo + name filter)
  - google_places ↔ lybrate   (name filter only)
  - practo        ↔ lybrate   (name filter only)
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone

import anthropic
from loguru import logger

from zelda.controllers.matching.graph import build_graph, rows_to_node_map
from zelda.controllers.matching.llm_judge import LLMJudge
from zelda.controllers.matching.prefilter import build_candidate_pairs
from zelda.controllers.matching.synthesis import SynthesisEngine
from zelda.models.lead import Lead
from zelda.models.matchable_row import (
    MatchableRow,
    from_google_places,
    from_lybrate,
    from_practo,
)
from zelda.repositories.google_places_lead_repo import GooglePlacesLeadRepository
from zelda.repositories.lead_repo import LeadRepository
from zelda.repositories.lybrate_listing_repo import LybrateListingRepository
from zelda.repositories.match_pair_repo import MatchPairRepository
from zelda.repositories.practo_listing_repo import PractoListingRepository


@dataclass
class MatchingResult:
    run_id: str
    city: str
    started_at: datetime
    finished_at: datetime | None = None

    # Stage 2/3 counts
    candidate_pairs: int = 0
    proposer_matches: int = 0
    reviewer_confirmed: int = 0

    # Stage 4
    conflicts_flagged: int = 0

    # Stage 6/7
    enriched_leads: int = 0
    standalone_leads: int = 0
    human_review_needed: int = 0

    errors: list[str] = field(default_factory=list)

    @property
    def total_leads(self) -> int:
        return self.enriched_leads + self.standalone_leads

    @property
    def source_rows(self) -> int:
        return 0  # populated by pipeline

    def summary(self) -> str:
        return (
            f"match {self.city}: run_id={self.run_id} "
            f"candidates={self.candidate_pairs} "
            f"proposer_matches={self.proposer_matches} "
            f"confirmed={self.reviewer_confirmed} "
            f"conflicts={self.conflicts_flagged} "
            f"enriched={self.enriched_leads} "
            f"standalone={self.standalone_leads} "
            f"total={self.total_leads} "
            f"human_review={self.human_review_needed} "
            f"errors={len(self.errors)}"
        )


class MatchingPipeline:
    def __init__(
        self,
        gp_repo: GooglePlacesLeadRepository,
        practo_repo: PractoListingRepository,
        lybrate_repo: LybrateListingRepository,
        lead_repo: LeadRepository,
        pair_repo: MatchPairRepository,
        anthropic_client: anthropic.Anthropic,
        *,
        geo_radius_km: float = 1.0,
        proposer_min_confidence: float = 0.75,
    ) -> None:
        self._gp_repo = gp_repo
        self._practo_repo = practo_repo
        self._lybrate_repo = lybrate_repo
        self._lead_repo = lead_repo
        self._pair_repo = pair_repo
        self._client = anthropic_client
        self._geo_radius_km = geo_radius_km
        self._proposer_min_confidence = proposer_min_confidence

    def run(self, city: str, *, run_id: str | None = None) -> MatchingResult:
        if not city or not city.strip():
            raise ValueError("city must be non-empty")

        rid = run_id or _make_run_id()
        now = datetime.now(timezone.utc)
        result = MatchingResult(run_id=rid, city=city, started_at=now)

        logger.info("match.start run_id={r} city={c}", r=rid, c=city)

        # ── Stage 1: Load + project ──────────────────────────────────
        gp_rows = [from_google_places(r) for r in self._gp_repo.get_for_city(city)]
        practo_rows = [from_practo(r) for r in self._practo_repo.get_for_city(city)]
        lybrate_rows = [from_lybrate(r) for r in self._lybrate_repo.get_for_city(city)]

        all_rows: list[MatchableRow] = gp_rows + practo_rows + lybrate_rows
        node_map = rows_to_node_map(all_rows)

        logger.info(
            "match.loaded city={c} gp={g} practo={p} lybrate={l}",
            c=city, g=len(gp_rows), p=len(practo_rows), l=len(lybrate_rows),
        )

        if not all_rows:
            result.finished_at = datetime.now(timezone.utc)
            return result

        # ── Stage 2: Pre-filter ──────────────────────────────────────
        source_pairs = [
            (gp_rows, practo_rows),
            (gp_rows, lybrate_rows),
            (practo_rows, lybrate_rows),
        ]
        all_candidates = []
        for rows_a, rows_b in source_pairs:
            pairs = build_candidate_pairs(
                rows_a, rows_b, geo_radius_km=self._geo_radius_km,
            )
            all_candidates.extend(pairs)

        result.candidate_pairs = len(all_candidates)
        logger.info(
            "match.prefilter city={c} candidates={n}", c=city, n=len(all_candidates),
        )

        # ── Stages 3 & 4: LLM Judge ─────────────────────────────────
        judge = LLMJudge(
            self._client,
            self._pair_repo,
            proposer_min_confidence=self._proposer_min_confidence,
        )
        confirmed_matches = []
        proposer_reasons: dict[tuple[str, str, str, str], str] = {}

        for pair in all_candidates:
            # Check proposer cache first to count proposer matches.
            proposer = self._pair_repo.get(
                pair.row_a.source, pair.row_a.key,
                pair.row_b.source, pair.row_b.key,
                "proposer",
            )
            reviewer = judge.evaluate_pair(pair)

            # Re-read proposer from cache (may have just been written).
            proposer = self._pair_repo.get(
                pair.row_a.source, pair.row_a.key,
                pair.row_b.source, pair.row_b.key,
                "proposer",
            )
            if proposer and proposer.match and (proposer.confidence or 0) >= self._proposer_min_confidence:
                result.proposer_matches += 1
                proposer_reasons[
                    (proposer.source_a, proposer.key_a, proposer.source_b, proposer.key_b)
                ] = proposer.reason

            if reviewer is not None:
                confirmed_matches.append(reviewer)
                result.reviewer_confirmed += 1

        logger.info(
            "match.judged city={c} proposer_matches={p} confirmed={r}",
            c=city, p=result.proposer_matches, r=result.reviewer_confirmed,
        )

        # ── Stage 5: Match graph + conflict detection ────────────────
        graph = build_graph(confirmed_matches, proposer_reasons)
        result.conflicts_flagged = sum(
            1 for e in graph._edges if e.human_review
        )

        # ── Stage 6: Synthesis + lead assembly ───────────────────────
        engine = SynthesisEngine(
            self._client,
            node_map,
            graph,
            city=city,
            run_id=rid,
        )
        leads: list[Lead] = engine.build_leads()

        result.enriched_leads = sum(1 for l in leads if l.tier == "enriched")
        result.standalone_leads = sum(1 for l in leads if l.tier == "standalone")
        result.human_review_needed = sum(1 for l in leads if l.human_review_needed)

        # ── Stage 7: Persist ─────────────────────────────────────────
        self._lead_repo.insert_many(leads)

        result.finished_at = datetime.now(timezone.utc)
        logger.info(result.summary())
        return result


def _make_run_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"match-{ts}-{secrets.token_hex(4)}"


__all__ = [
    "MatchingPipeline",
    "MatchingResult",
]
