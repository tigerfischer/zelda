"""Tests for Stage 2+3 — LLM judge (Proposer + Reviewer).

The Anthropic client is mocked so no real API calls are made. Tests
verify caching behaviour, confidence thresholding, and the flow where
Proposer accepts but Reviewer rejects.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from zelda.controllers.matching.llm_judge import LLMJudge, PROPOSER_MIN_CONFIDENCE
from zelda.controllers.matching.prefilter import CandidatePair
from zelda.models.matchable_row import MatchableRow
from zelda.repositories.match_pair_repo import MatchPairRepository


_T = datetime(2026, 4, 30, tzinfo=timezone.utc)

_GP_LAT, _GP_LNG = 30.9010, 75.8573


def _row(source: str, key: str, name: str) -> MatchableRow:
    return MatchableRow(
        source=source, key=key, name=name, city="Ludhiana",
        lat=_GP_LAT, lng=_GP_LNG,
    )


def _pair(
    source_a: str = "google_places",
    key_a: str = "gp1",
    source_b: str = "practo",
    key_b: str = "pr1",
) -> CandidatePair:
    return CandidatePair(
        row_a=_row(source_a, key_a, "Puri Dental"),
        row_b=_row(source_b, key_b, "Puri Clinic"),
        geo_distance_km=0.15,
        passed_geo=True,
        passed_name=True,
    )


def _fake_response(tool_input: dict[str, Any]) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.input = tool_input
    msg = MagicMock()
    msg.content = [block]
    return msg


@pytest.fixture
def pair_repo():
    r = MatchPairRepository(":memory:")
    yield r
    r.close()


@pytest.fixture
def mock_client():
    return MagicMock()


def _make_judge(mock_client, pair_repo, min_confidence=PROPOSER_MIN_CONFIDENCE):
    return LLMJudge(mock_client, pair_repo, proposer_min_confidence=min_confidence)


# ── proposer accepts, reviewer confirms ─────────────────────────────

def test_both_agree_returns_reviewer_evaluation(mock_client, pair_repo):
    mock_client.messages.create.side_effect = [
        _fake_response({"match": True, "confidence": 0.9, "reason": "same address"}),
        _fake_response({"agree": True, "reason": "I agree"}),
    ]
    judge = _make_judge(mock_client, pair_repo)
    result = judge.evaluate_pair(_pair())

    assert result is not None
    assert result.match is True
    assert result.stage == "reviewer"
    assert mock_client.messages.create.call_count == 2


# ── proposer below confidence threshold ─────────────────────────────

def test_low_confidence_proposer_skips_reviewer(mock_client, pair_repo):
    mock_client.messages.create.return_value = _fake_response(
        {"match": True, "confidence": 0.5, "reason": "weak signal"}
    )
    judge = _make_judge(mock_client, pair_repo)
    result = judge.evaluate_pair(_pair())

    assert result is None
    assert mock_client.messages.create.call_count == 1  # Reviewer never called


# ── proposer rejects ────────────────────────────────────────────────

def test_proposer_no_match_skips_reviewer(mock_client, pair_repo):
    mock_client.messages.create.return_value = _fake_response(
        {"match": False, "confidence": 0.95, "reason": "different buildings"}
    )
    judge = _make_judge(mock_client, pair_repo)
    result = judge.evaluate_pair(_pair())

    assert result is None
    assert mock_client.messages.create.call_count == 1


# ── reviewer rejects ────────────────────────────────────────────────

def test_reviewer_disagree_returns_none(mock_client, pair_repo):
    mock_client.messages.create.side_effect = [
        _fake_response({"match": True, "confidence": 0.85, "reason": "same name area"}),
        _fake_response({"agree": False, "reason": "different phone numbers"}),
    ]
    judge = _make_judge(mock_client, pair_repo)
    result = judge.evaluate_pair(_pair())

    assert result is None
    assert mock_client.messages.create.call_count == 2


# ── caching ──────────────────────────────────────────────────────────

def test_cached_proposer_not_re_called(mock_client, pair_repo):
    mock_client.messages.create.side_effect = [
        _fake_response({"match": True, "confidence": 0.9, "reason": "same address"}),
        _fake_response({"agree": True, "reason": "confirmed"}),
    ]
    judge = _make_judge(mock_client, pair_repo)
    pair = _pair()

    judge.evaluate_pair(pair)
    call_count_after_first = mock_client.messages.create.call_count

    # Second call — proposer result is cached, only Reviewer is called again
    # (Reviewer is also cached on second call)
    judge.evaluate_pair(pair)

    # Both proposer and reviewer are cached now — no new calls
    assert mock_client.messages.create.call_count == call_count_after_first


def test_results_persisted_to_repo(mock_client, pair_repo):
    mock_client.messages.create.side_effect = [
        _fake_response({"match": True, "confidence": 0.9, "reason": "same phone"}),
        _fake_response({"agree": True, "reason": "confirmed"}),
    ]
    judge = _make_judge(mock_client, pair_repo)
    judge.evaluate_pair(_pair())

    proposer = pair_repo.get("google_places", "gp1", "practo", "pr1", "proposer")
    reviewer = pair_repo.get("google_places", "gp1", "practo", "pr1", "reviewer")
    assert proposer is not None
    assert reviewer is not None
    assert proposer.match is True
    assert reviewer.match is True
