"""SQLite cache for `MatchPairEvaluation`.

LLM calls are expensive. This repo persists every Proposer and Reviewer
verdict so re-running the matching pipeline (after tuning thresholds,
adding a city, etc.) skips pairs already evaluated.

Cache key: (source_a, key_a, source_b, key_b, stage) — always stored with
(source_a, key_a) as the lexicographically earlier member of the pair so
the same pair has exactly one row regardless of which direction triggered it.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from zelda.db import connect as _db_connect
from zelda.models.match_pair import MatchPairEvaluation


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS match_pair_evaluations (
    source_a     TEXT NOT NULL,
    key_a        TEXT NOT NULL,
    source_b     TEXT NOT NULL,
    key_b        TEXT NOT NULL,
    stage        TEXT NOT NULL CHECK(stage IN ('proposer', 'reviewer')),
    match        INTEGER NOT NULL,
    confidence   REAL,
    reason       TEXT NOT NULL,
    model        TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    PRIMARY KEY (source_a, key_a, source_b, key_b, stage)
);
"""

# Canonical ordering so (A, B) and (B, A) map to the same row.
_SOURCE_ORDER = {"google_places": 0, "practo": 1, "lybrate": 2}


def _canonical(
    source_a: str, key_a: str, source_b: str, key_b: str,
) -> tuple[str, str, str, str]:
    if _SOURCE_ORDER.get(source_a, 99) > _SOURCE_ORDER.get(source_b, 99):
        return source_b, key_b, source_a, key_a
    return source_a, key_a, source_b, key_b


class MatchPairRepository:
    """Cache for LLM pair evaluations. Single-threaded use only."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = _db_connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self.init_schema()

    def __enter__(self) -> "MatchPairRepository":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA_SQL)

    # ── reads ────────────────────────────────────────────────────────

    def get(
        self,
        source_a: str, key_a: str,
        source_b: str, key_b: str,
        stage: str,
    ) -> MatchPairEvaluation | None:
        sa, ka, sb, kb = _canonical(source_a, key_a, source_b, key_b)
        row = self._conn.execute(
            "SELECT * FROM match_pair_evaluations "
            "WHERE source_a=? AND key_a=? AND source_b=? AND key_b=? AND stage=?",
            (sa, ka, sb, kb, stage),
        ).fetchone()
        return _row_to_eval(row) if row else None

    def get_confirmed_matches(
        self, city_keys: set[str] | None = None,
    ) -> list[MatchPairEvaluation]:
        """Return all reviewer-confirmed matches (both stages agree match=True).

        If `city_keys` is provided, filter to pairs where key_a or key_b is
        in the set (caller builds the set from city rows).
        """
        rows = self._conn.execute(
            "SELECT p.source_a, p.key_a, p.source_b, p.key_b, "
            "       r.match, r.confidence, r.reason, r.model, r.evaluated_at "
            "FROM match_pair_evaluations p "
            "JOIN match_pair_evaluations r "
            "  ON  r.source_a = p.source_a AND r.key_a = p.key_a "
            "  AND r.source_b = p.source_b AND r.key_b = p.key_b "
            "  AND r.stage = 'reviewer' "
            "WHERE p.stage = 'proposer' "
            "  AND p.match = 1 AND r.match = 1",
        ).fetchall()
        results = [
            MatchPairEvaluation(
                source_a=r["source_a"], key_a=r["key_a"],
                source_b=r["source_b"], key_b=r["key_b"],
                stage="reviewer",
                match=bool(r["match"]),
                confidence=r["confidence"],
                reason=r["reason"],
                model=r["model"],
                evaluated_at=datetime.fromisoformat(r["evaluated_at"]),
            )
            for r in rows
        ]
        if city_keys:
            results = [
                e for e in results
                if e.key_a in city_keys or e.key_b in city_keys
            ]
        return results

    # ── writes ───────────────────────────────────────────────────────

    def save(self, evaluation: MatchPairEvaluation) -> None:
        sa, ka, sb, kb = _canonical(
            evaluation.source_a, evaluation.key_a,
            evaluation.source_b, evaluation.key_b,
        )
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO match_pair_evaluations
                    (source_a, key_a, source_b, key_b, stage,
                     match, confidence, reason, model, evaluated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_a, key_a, source_b, key_b, stage)
                DO UPDATE SET
                    match=excluded.match,
                    confidence=excluded.confidence,
                    reason=excluded.reason,
                    model=excluded.model,
                    evaluated_at=excluded.evaluated_at
                """,
                (
                    sa, ka, sb, kb, evaluation.stage,
                    int(evaluation.match), evaluation.confidence,
                    evaluation.reason, evaluation.model,
                    evaluation.evaluated_at.isoformat(),
                ),
            )

    def count_by_stage(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT stage, COUNT(*) FROM match_pair_evaluations GROUP BY stage"
        ).fetchall()
        return {r[0]: r[1] for r in rows}


def _row_to_eval(row: sqlite3.Row) -> MatchPairEvaluation:
    return MatchPairEvaluation(
        source_a=row["source_a"],
        key_a=row["key_a"],
        source_b=row["source_b"],
        key_b=row["key_b"],
        stage=row["stage"],
        match=bool(row["match"]),
        confidence=row["confidence"],
        reason=row["reason"],
        model=row["model"],
        evaluated_at=datetime.fromisoformat(row["evaluated_at"]),
    )


__all__ = ["MatchPairRepository"]
