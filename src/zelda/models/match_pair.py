"""`MatchPairEvaluation` — one LLM judge's verdict on a (row_A, row_B) pair.

These are persisted as a cache so re-running the pipeline after a config
change (e.g. new city, tuned confidence threshold) doesn't re-call the LLM
for pairs already evaluated. The cache key is:
    (source_a, key_a, source_b, key_b, stage)
with (source_a, key_a) always the lexicographically smaller member of the
pair, so the same pair stored in either order has one canonical row.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class MatchPairEvaluation(BaseModel):
    source_a: str
    key_a: str
    source_b: str
    key_b: str
    stage: Literal["proposer", "reviewer"]

    match: bool
    confidence: float | None = Field(
        default=None,
        description="Proposer only — 0.0..1.0. None for reviewer stage.",
    )
    reason: str

    model: str
    evaluated_at: datetime


__all__ = ["MatchPairEvaluation"]
