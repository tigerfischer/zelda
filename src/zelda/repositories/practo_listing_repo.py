"""SQLite-backed persistence for `PractoListing`.

One row per Practo profile URL. The discovery pipeline upserts these
once per city; cross-source matching reads them later. Same delta-
detection contract as `GooglePlacesLeadRepository` — sync pushes only
rows where `last_synced_at IS NULL OR last_modified_at > last_synced_at`.

Schema notes
------------
- `profile_url` is the natural PK (Practo's stable per-clinic URL).
- `city` is denormalized onto each row so per-city queries are
  O(rows-in-city), not O(table).
- `raw_json` holds the original extracted payload for forward
  compatibility (e.g. when we promote a new field to a column).
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from zelda.db import connect as _db_connect
from typing import Any, Iterable

from zelda.models.practo_listing import PractoListing


_COLUMNS: tuple[str, ...] = (
    "profile_url",
    "city",
    "name",
    "address",
    "lat",
    "lng",
    "raw_json",
    "discovered_at",
    "last_modified_at",
    "last_synced_at",
)

# Owned by another concern — never touched by upsert.
_UPSERT_IMMUTABLE: frozenset[str] = frozenset({
    "profile_url",      # natural key
    "discovered_at",    # immutable per row
    "last_synced_at",   # owned by sync
})


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS practo_listings (
    profile_url       TEXT PRIMARY KEY,
    city              TEXT NOT NULL,
    name              TEXT NOT NULL,
    address           TEXT,
    lat               REAL,
    lng               REAL,
    raw_json          TEXT NOT NULL DEFAULT '{}',
    discovered_at     TEXT NOT NULL,
    last_modified_at  TEXT NOT NULL,
    last_synced_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_practo_listings_city
    ON practo_listings(city);
CREATE INDEX IF NOT EXISTS idx_practo_listings_sync
    ON practo_listings(city, last_modified_at, last_synced_at);
"""


class PractoListingRepository:
    """SQLite repository for `PractoListing`. Single-threaded use only."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = _db_connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self.init_schema()

    # ── lifecycle ────────────────────────────────────────────────────

    def __enter__(self) -> "PractoListingRepository":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA_SQL)

    # ── reads ────────────────────────────────────────────────────────

    def exists(self, profile_url: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM practo_listings WHERE profile_url = ? LIMIT 1",
            (profile_url,),
        )
        return cur.fetchone() is not None

    def exists_many(self, profile_urls: Iterable[str]) -> set[str]:
        urls = list(profile_urls)
        if not urls:
            return set()
        placeholders = ",".join("?" * len(urls))
        cur = self._conn.execute(
            f"SELECT profile_url FROM practo_listings "
            f"WHERE profile_url IN ({placeholders})",
            urls,
        )
        return {row[0] for row in cur.fetchall()}

    def get_by_url(self, profile_url: str) -> PractoListing | None:
        cur = self._conn.execute(
            "SELECT * FROM practo_listings WHERE profile_url = ?",
            (profile_url,),
        )
        row = cur.fetchone()
        return _row_to_listing(row) if row else None

    def get_for_city(self, city: str) -> list[PractoListing]:
        cur = self._conn.execute(
            "SELECT * FROM practo_listings WHERE city = ? "
            "ORDER BY discovered_at ASC",
            (city,),
        )
        return [_row_to_listing(row) for row in cur.fetchall()]

    def get_unsynced_for_city(self, city: str) -> list[PractoListing]:
        cur = self._conn.execute(
            """
            SELECT * FROM practo_listings
            WHERE city = ?
              AND (last_synced_at IS NULL OR last_modified_at > last_synced_at)
            ORDER BY discovered_at ASC
            """,
            (city,),
        )
        return [_row_to_listing(row) for row in cur.fetchall()]

    def count_for_city(self, city: str) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM practo_listings WHERE city = ?", (city,),
        )
        return int(cur.fetchone()[0])

    def count_unsynced_for_city(self, city: str) -> int:
        cur = self._conn.execute(
            """
            SELECT COUNT(*) FROM practo_listings
            WHERE city = ?
              AND (last_synced_at IS NULL OR last_modified_at > last_synced_at)
            """,
            (city,),
        )
        return int(cur.fetchone()[0])

    # ── writes ───────────────────────────────────────────────────────

    def upsert_many(self, listings: Iterable[PractoListing]) -> None:
        rows = [_listing_to_row(l) for l in listings]
        if not rows:
            return
        with self._conn:
            self._conn.executemany(_upsert_sql(), rows)

    def mark_synced(
        self,
        profile_urls: Iterable[str],
        *,
        synced_at: datetime | None = None,
    ) -> None:
        urls = list(profile_urls)
        if not urls:
            return
        ts = (synced_at or datetime.now(timezone.utc)).isoformat()
        with self._conn:
            self._conn.executemany(
                "UPDATE practo_listings SET last_synced_at = ? "
                "WHERE profile_url = ?",
                [(ts, u) for u in urls],
            )


# ── module-private helpers ───────────────────────────────────────────


def _upsert_sql() -> str:
    cols = ", ".join(_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in _COLUMNS)
    update_cols = [c for c in _COLUMNS if c not in _UPSERT_IMMUTABLE]
    update_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
    return (
        f"INSERT INTO practo_listings ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(profile_url) DO UPDATE SET {update_clause}"
    )


def _listing_to_row(l: PractoListing) -> dict[str, Any]:
    return {
        "profile_url": l.profile_url,
        "city": l.city,
        "name": l.name,
        "address": l.address,
        "lat": l.lat,
        "lng": l.lng,
        "raw_json": json.dumps(l.raw_json),
        "discovered_at": l.discovered_at.isoformat(),
        "last_modified_at": l.last_modified_at.isoformat(),
        "last_synced_at": (
            l.last_synced_at.isoformat() if l.last_synced_at else None
        ),
    }


def _row_to_listing(row: sqlite3.Row) -> PractoListing:
    return PractoListing(
        profile_url=row["profile_url"],
        city=row["city"],
        name=row["name"],
        address=row["address"],
        lat=row["lat"],
        lng=row["lng"],
        raw_json=json.loads(row["raw_json"]) if row["raw_json"] else {},
        discovered_at=datetime.fromisoformat(row["discovered_at"]),
        last_modified_at=datetime.fromisoformat(row["last_modified_at"]),
        last_synced_at=(
            datetime.fromisoformat(row["last_synced_at"])
            if row["last_synced_at"] else None
        ),
    )
