"""SQLite-backed persistence for `ReviewSet` and `Review`.

Two tables:

- `review_captures` — one row per scrape run per place_id. Holds the
  metadata that travels with a `ReviewSet` (truncation flag, total
  per GBP, capture order, status, error message, captured_at). This
  is the bookkeeping that lets downstream code answer "did this
  capture cover the whole window?"

- `reviews` — one row per (place_id, review_id). Each review records
  which capture last saw it (`last_seen_capture_id`) and when first
  / last observed. UPSERT semantics preserve `discovered_at` from
  the first sighting.

Why two tables and not one wide one?
- A single capture run produces N rows in `reviews` and 1 row in
  `review_captures`; the metadata isn't N-fold redundant on every
  review.
- Refresh runs see overlap with previous captures; the join lets us
  cheaply ask "what new reviews did the latest capture pull?".

`save_capture(review_set)` is the single write entry point. It writes
the capture metadata + upserts every review in one transaction. Any
downstream consumer reading reviews gets back a `ReviewSet` so the
bounds metadata flows through correctly.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from zelda.db import connect as _db_connect
from zelda.models.review import Review, ReviewSet


_REVIEW_CAPTURE_COLUMNS: tuple[str, ...] = (
    "capture_id",
    "place_id",
    "captured_at",
    "capture_cap",
    "capture_order",
    "total_reviews_per_gbp",
    "reviews_captured",
    "is_truncated",
    "earliest_review_at",
    "latest_review_at",
    "fetch_status",
    "error_message",
)

_REVIEW_COLUMNS: tuple[str, ...] = (
    "review_id",
    "place_id",
    "first_seen_capture_id",
    "last_seen_capture_id",
    "discovered_at",
    "last_seen_at",
    "rating",
    "text",
    "language",
    "author_name",
    "author_url",
    "author_photo_url",
    "relative_publish_time",
    "approx_publish_at",
    "owner_response_text",
    "owner_response_relative_time",
    "owner_response_approx_at",
    "photo_urls",
    "likes_count",
    "sequence_in_capture",
    "raw_json",
)

# On UPSERT into `reviews`, never overwrite these — they record the
# original sighting and are immutable per (place_id, review_id).
_REVIEW_UPSERT_IMMUTABLE: frozenset[str] = frozenset({
    "place_id",
    "review_id",
    "first_seen_capture_id",
    "discovered_at",
})

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS review_captures (
    capture_id              TEXT PRIMARY KEY,
    place_id                TEXT NOT NULL,
    captured_at             TEXT NOT NULL,
    capture_cap             INTEGER NOT NULL,
    capture_order           TEXT NOT NULL,
    total_reviews_per_gbp   INTEGER,
    reviews_captured        INTEGER NOT NULL,
    is_truncated            INTEGER NOT NULL,
    earliest_review_at      TEXT,
    latest_review_at        TEXT,
    fetch_status            TEXT NOT NULL,
    error_message           TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_captures_place
    ON review_captures(place_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS reviews (
    review_id                       TEXT NOT NULL,
    place_id                        TEXT NOT NULL,
    first_seen_capture_id           TEXT NOT NULL,
    last_seen_capture_id            TEXT NOT NULL,
    discovered_at                   TEXT NOT NULL,
    last_seen_at                    TEXT NOT NULL,
    rating                          INTEGER,
    text                            TEXT,
    language                        TEXT,
    author_name                     TEXT,
    author_url                      TEXT,
    author_photo_url                TEXT,
    relative_publish_time           TEXT,
    approx_publish_at               TEXT,
    owner_response_text             TEXT,
    owner_response_relative_time    TEXT,
    owner_response_approx_at        TEXT,
    photo_urls                      TEXT NOT NULL DEFAULT '[]',
    likes_count                     INTEGER,
    sequence_in_capture             INTEGER,
    raw_json                        TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (place_id, review_id)
);

CREATE INDEX IF NOT EXISTS idx_reviews_place_seq
    ON reviews(place_id, sequence_in_capture);
CREATE INDEX IF NOT EXISTS idx_reviews_place_publish
    ON reviews(place_id, approx_publish_at);
CREATE INDEX IF NOT EXISTS idx_reviews_last_capture
    ON reviews(last_seen_capture_id);
"""


class ReviewRepository:
    """SQLite repository for `Review` and `ReviewSet`. Single-threaded
    use only."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = _db_connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self.init_schema()

    # ── lifecycle ────────────────────────────────────────────────────

    def __enter__(self) -> "ReviewRepository":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def init_schema(self) -> None:
        """Idempotent schema creation."""
        with self._conn:
            self._conn.executescript(_SCHEMA_SQL)

    # ── writes ───────────────────────────────────────────────────────

    def save_capture(self, review_set: ReviewSet, *, capture_id: str) -> None:
        """Write `review_set` to the DB as one capture run.

        - Inserts a row in `review_captures` for the run's metadata.
        - Upserts each review in `review_set.reviews` into `reviews`.
          On conflict, preserves `first_seen_capture_id` and
          `discovered_at`; refreshes `last_seen_capture_id`,
          `last_seen_at`, and any mutable fields.

        `capture_id` is supplied by the caller (controller) so it ties
        to the JSONL artifact filename / run identifier.
        """
        if not capture_id or not capture_id.strip():
            raise ValueError("capture_id must be non-empty")

        capture_row = _capture_to_row(review_set, capture_id)
        review_rows = [
            _review_to_row(r, capture_id=capture_id, captured_at=review_set.captured_at)
            for r in review_set.reviews
        ]

        with self._conn:
            self._conn.execute(
                _capture_insert_sql(), capture_row,
            )
            if review_rows:
                self._conn.executemany(
                    _review_upsert_sql(), review_rows,
                )

    # ── reads ────────────────────────────────────────────────────────

    def get_latest_capture(self, place_id: str) -> dict | None:
        """Return the most recent capture's metadata row for `place_id`,
        or None if no captures exist."""
        cur = self._conn.execute(
            """
            SELECT * FROM review_captures
            WHERE place_id = ?
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (place_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_reviews_for_place(self, place_id: str) -> list[Review]:
        """Return every stored review for `place_id`, newest first by
        approx_publish_at (with NULLs sorting last)."""
        cur = self._conn.execute(
            """
            SELECT * FROM reviews
            WHERE place_id = ?
            ORDER BY (approx_publish_at IS NULL), approx_publish_at DESC,
                     sequence_in_capture ASC
            """,
            (place_id,),
        )
        return [_row_to_review(row) for row in cur.fetchall()]

    def count_reviews_for_place(self, place_id: str) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE place_id = ?", (place_id,),
        )
        return int(cur.fetchone()[0])

    def count_captures_for_place(self, place_id: str) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM review_captures WHERE place_id = ?", (place_id,),
        )
        return int(cur.fetchone()[0])

    def get_known_review_ids_for_place(self, place_id: str) -> set[str]:
        """Useful for the controller's stop-when-we-see-a-known-review
        logic during incremental refresh."""
        cur = self._conn.execute(
            "SELECT review_id FROM reviews WHERE place_id = ?", (place_id,),
        )
        return {row[0] for row in cur.fetchall()}


# ── module-private helpers ──────────────────────────────────────────


def _capture_insert_sql() -> str:
    cols = ", ".join(_REVIEW_CAPTURE_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in _REVIEW_CAPTURE_COLUMNS)
    return (
        f"INSERT OR REPLACE INTO review_captures ({cols}) "
        f"VALUES ({placeholders})"
    )


def _review_upsert_sql() -> str:
    cols = ", ".join(_REVIEW_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in _REVIEW_COLUMNS)
    update_cols = [c for c in _REVIEW_COLUMNS if c not in _REVIEW_UPSERT_IMMUTABLE]
    update_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
    return (
        f"INSERT INTO reviews ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(place_id, review_id) DO UPDATE SET {update_clause}"
    )


def _capture_to_row(rs: ReviewSet, capture_id: str) -> dict[str, Any]:
    return {
        "capture_id": capture_id,
        "place_id": rs.place_id,
        "captured_at": rs.captured_at.isoformat(),
        "capture_cap": rs.capture_cap,
        "capture_order": rs.capture_order,
        "total_reviews_per_gbp": rs.total_reviews_per_gbp,
        "reviews_captured": rs.reviews_captured,
        "is_truncated": int(rs.is_truncated),
        "earliest_review_at": (
            rs.earliest_review_at.isoformat() if rs.earliest_review_at else None
        ),
        "latest_review_at": (
            rs.latest_review_at.isoformat() if rs.latest_review_at else None
        ),
        "fetch_status": rs.fetch_status,
        "error_message": rs.error_message,
    }


def _review_to_row(
    review: Review, *, capture_id: str, captured_at: datetime,
) -> dict[str, Any]:
    """Build the SQL row dict for an UPSERT.

    `first_seen_capture_id` and `discovered_at` are bound to this
    capture; on conflict the SQL preserves them via the immutable list.
    """
    captured_iso = captured_at.isoformat()
    return {
        "review_id": review.review_id,
        "place_id": review.place_id,
        "first_seen_capture_id": capture_id,
        "last_seen_capture_id": capture_id,
        "discovered_at": captured_iso,
        "last_seen_at": captured_iso,
        "rating": review.rating,
        "text": review.text,
        "language": review.language,
        "author_name": review.author_name,
        "author_url": review.author_url,
        "author_photo_url": review.author_photo_url,
        "relative_publish_time": review.relative_publish_time,
        "approx_publish_at": (
            review.approx_publish_at.isoformat() if review.approx_publish_at else None
        ),
        "owner_response_text": review.owner_response_text,
        "owner_response_relative_time": review.owner_response_relative_time,
        "owner_response_approx_at": (
            review.owner_response_approx_at.isoformat()
            if review.owner_response_approx_at else None
        ),
        "photo_urls": json.dumps(review.photo_urls),
        "likes_count": review.likes_count,
        "sequence_in_capture": review.sequence_in_capture,
        "raw_json": json.dumps(review.raw_json),
    }


def _row_to_review(row: sqlite3.Row) -> Review:
    return Review(
        review_id=row["review_id"],
        place_id=row["place_id"],
        rating=row["rating"],
        text=row["text"],
        language=row["language"],
        author_name=row["author_name"],
        author_url=row["author_url"],
        author_photo_url=row["author_photo_url"],
        relative_publish_time=row["relative_publish_time"],
        approx_publish_at=(
            datetime.fromisoformat(row["approx_publish_at"])
            if row["approx_publish_at"] else None
        ),
        owner_response_text=row["owner_response_text"],
        owner_response_relative_time=row["owner_response_relative_time"],
        owner_response_approx_at=(
            datetime.fromisoformat(row["owner_response_approx_at"])
            if row["owner_response_approx_at"] else None
        ),
        photo_urls=json.loads(row["photo_urls"]) if row["photo_urls"] else [],
        likes_count=row["likes_count"],
        sequence_in_capture=row["sequence_in_capture"],
        raw_json=json.loads(row["raw_json"]) if row["raw_json"] else {},
    )
