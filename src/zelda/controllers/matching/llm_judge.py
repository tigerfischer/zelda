"""Stages 2 & 3 — LLM Proposer and Reviewer.

Proposer (Haiku — high volume, cheap):
  For each candidate pair, asks: "Same clinic? Yes/No + confidence + reason."
  Pairs below PROPOSER_MIN_CONFIDENCE are dropped before the Reviewer sees them.

Reviewer (Sonnet — higher stakes):
  For every pair the Proposer accepted, independently reviews the same pair
  plus the Proposer's reasoning. Both must agree for a match to be confirmed.

Both use Anthropic tool_use to enforce structured JSON output — more reliable
than asking for JSON in the message body.

Caching:
  Every verdict is saved to `MatchPairRepository` before returning. On a
  re-run, cached verdicts are returned immediately (no API call).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import anthropic
from loguru import logger

from zelda.controllers.matching.prefilter import CandidatePair
from zelda.controllers.matching.prompt_loader import render_prompt
from zelda.models.match_pair import MatchPairEvaluation
from zelda.models.matchable_row import MatchableRow
from zelda.repositories.match_pair_repo import MatchPairRepository


PROPOSER_MODEL = "claude-haiku-4-5-20251001"
REVIEWER_MODEL = "claude-sonnet-4-6"
PROPOSER_MIN_CONFIDENCE: float = 0.75


# ── tool schemas ────────────────────────────────────────────────────

_PROPOSER_TOOL: dict[str, Any] = {
    "name": "match_assessment",
    "description": "Record whether two dental clinic listings refer to the same physical clinic.",
    "input_schema": {
        "type": "object",
        "properties": {
            "match": {
                "type": "boolean",
                "description": "True if both listings refer to the same physical clinic.",
            },
            "confidence": {
                "type": "number",
                "description": "Confidence in the match decision, 0.0 (none) to 1.0 (certain).",
            },
            "reason": {
                "type": "string",
                "description": "Brief explanation (1-3 sentences) of the key evidence.",
            },
        },
        "required": ["match", "confidence", "reason"],
    },
}

_REVIEWER_TOOL: dict[str, Any] = {
    "name": "review_assessment",
    "description": "Record independent review of a proposed clinic match.",
    "input_schema": {
        "type": "object",
        "properties": {
            "agree": {
                "type": "boolean",
                "description": "True if you agree the two listings are the same clinic.",
            },
            "reason": {
                "type": "string",
                "description": "Brief explanation of your independent assessment.",
            },
        },
        "required": ["agree", "reason"],
    },
}


# ── prompt builders ─────────────────────────────────────────────────

def _proposer_prompt(pair: CandidatePair) -> str:
    return render_prompt(
        "matching/proposer.j2",
        row_a=pair.row_a,
        row_b=pair.row_b,
        geo_distance_km=pair.geo_distance_km,
    )


def _reviewer_prompt(pair: CandidatePair, proposer_result: MatchPairEvaluation) -> str:
    return render_prompt(
        "matching/reviewer.j2",
        row_a=pair.row_a,
        row_b=pair.row_b,
        geo_distance_km=pair.geo_distance_km,
        proposer_confidence=proposer_result.confidence or 0.0,
        proposer_reason=proposer_result.reason,
    )


# ── judge class ─────────────────────────────────────────────────────

class LLMJudge:
    """Runs Proposer and Reviewer LLM calls, with SQLite caching."""

    def __init__(
        self,
        client: anthropic.Anthropic,
        pair_repo: MatchPairRepository,
        *,
        proposer_model: str = PROPOSER_MODEL,
        reviewer_model: str = REVIEWER_MODEL,
        proposer_min_confidence: float = PROPOSER_MIN_CONFIDENCE,
    ) -> None:
        self._client = client
        self._pair_repo = pair_repo
        self._proposer_model = proposer_model
        self._reviewer_model = reviewer_model
        self._min_confidence = proposer_min_confidence

    def evaluate_pair(self, pair: CandidatePair) -> MatchPairEvaluation | None:
        """Run Proposer then Reviewer for one candidate pair.

        Returns the Reviewer's `MatchPairEvaluation` if both agreed on a match,
        None if either stage rejected the pair.

        Caches all intermediate results — safe to call repeatedly.
        """
        proposer = self._propose(pair)
        if not proposer.match or (proposer.confidence or 0) < self._min_confidence:
            logger.debug(
                "judge.proposer_rejected pair=({sa},{ka})↔({sb},{kb}) "
                "match={m} confidence={c:.2f}",
                sa=pair.row_a.source, ka=pair.row_a.key[:20],
                sb=pair.row_b.source, kb=pair.row_b.key[:20],
                m=proposer.match, c=proposer.confidence or 0,
            )
            return None

        reviewer = self._review(pair, proposer)
        if not reviewer.match:
            logger.debug(
                "judge.reviewer_rejected pair=({sa},{ka})↔({sb},{kb})",
                sa=pair.row_a.source, ka=pair.row_a.key[:20],
                sb=pair.row_b.source, kb=pair.row_b.key[:20],
            )
            return None

        logger.info(
            "judge.confirmed pair=({sa},{ka})↔({sb},{kb}) "
            "confidence={c:.2f}",
            sa=pair.row_a.source, ka=pair.row_a.key[:20],
            sb=pair.row_b.source, kb=pair.row_b.key[:20],
            c=proposer.confidence or 0,
        )
        return reviewer

    # ── private ─────────────────────────────────────────────────────

    def _propose(self, pair: CandidatePair) -> MatchPairEvaluation:
        cached = self._pair_repo.get(
            pair.row_a.source, pair.row_a.key,
            pair.row_b.source, pair.row_b.key,
            "proposer",
        )
        if cached is not None:
            return cached

        response = self._client.messages.create(
            model=self._proposer_model,
            max_tokens=512,
            tools=[_PROPOSER_TOOL],
            tool_choice={"type": "tool", "name": "match_assessment"},
            messages=[{"role": "user", "content": _proposer_prompt(pair)}],
        )
        tool_input = _extract_tool_input(response)
        result = MatchPairEvaluation(
            source_a=pair.row_a.source,
            key_a=pair.row_a.key,
            source_b=pair.row_b.source,
            key_b=pair.row_b.key,
            stage="proposer",
            match=bool(tool_input["match"]),
            confidence=float(tool_input["confidence"]),
            reason=str(tool_input["reason"]),
            model=self._proposer_model,
            evaluated_at=datetime.now(timezone.utc),
        )
        self._pair_repo.save(result)
        return result

    def _review(
        self,
        pair: CandidatePair,
        proposer_result: MatchPairEvaluation,
    ) -> MatchPairEvaluation:
        cached = self._pair_repo.get(
            pair.row_a.source, pair.row_a.key,
            pair.row_b.source, pair.row_b.key,
            "reviewer",
        )
        if cached is not None:
            return cached

        response = self._client.messages.create(
            model=self._reviewer_model,
            max_tokens=512,
            tools=[_REVIEWER_TOOL],
            tool_choice={"type": "tool", "name": "review_assessment"},
            messages=[{"role": "user", "content": _reviewer_prompt(pair, proposer_result)}],
        )
        tool_input = _extract_tool_input(response)
        result = MatchPairEvaluation(
            source_a=pair.row_a.source,
            key_a=pair.row_a.key,
            source_b=pair.row_b.source,
            key_b=pair.row_b.key,
            stage="reviewer",
            match=bool(tool_input["agree"]),
            confidence=proposer_result.confidence,
            reason=str(tool_input["reason"]),
            model=self._reviewer_model,
            evaluated_at=datetime.now(timezone.utc),
        )
        self._pair_repo.save(result)
        return result


def _extract_tool_input(response: anthropic.types.Message) -> dict[str, Any]:
    for block in response.content:
        if block.type == "tool_use":
            return block.input  # type: ignore[return-value]
    raise ValueError(f"No tool_use block in LLM response: {response.content}")


__all__ = [
    "PROPOSER_MODEL",
    "REVIEWER_MODEL",
    "PROPOSER_MIN_CONFIDENCE",
    "LLMJudge",
]
