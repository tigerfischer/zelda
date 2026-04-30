"""SQLite-backed persistence for `LybrateListing`.

One row per Lybrate doctor profile URL. Same shape and contract as
`PractoListingRepository` (per-source listing table, delta-based sync,
city-scoped queries) — Lybrate just has more fields exposed in its
JSON-LD so the table is wider.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from zelda.models.lybrate_listing import LybrateListing


_COLUMNS: tuple[str, ...] = (
    "profile_url",
    "city",
    "doctor_name",
    "clinic_name",
    "address",
    "locality",
    "postal_code",
    "lat",
    "lng",
    "phone",
    "specialty",
    "raw_json",
    "discovered_at",
    "last_modified_at",
    "last_synced_at",
)

_UPSERT_IMMUTABLE: frozenset[str] = frozenset({
    "profile_url",      # natural key
    "discovered_at",    # immutable per row
    "last_synced_at",   # owned by sync
})


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS lybrate_listings (
    profile_url       TEXT PRIMARY KEY,
    city              TEXT NOT NULL,
    doctor_name       TEXT NOT NULL,
    clinic_name       TEXT,
    address           TEXT,
    locality          TEXT,
    postal_code       TEXT,
    lat               REAL,
    lng               REAL,
    phone             TEXT,
    specialty         TEXT,
    raw_json          TEXT NOT NULL DEFAULT '{}',
    discovered_at     TEXT NOT NULL,
    last_modified_at  TEXT NOT NULL,
    last_synced_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_lybrate_listings_city
    ON lybrate_listings(city);
CREATE INDEX IF NOT EXISTS idx_lybrate_listings_sync
    ON lybrate_listings(city, last_modified_at, last_synced_at);
"""


class LybrateListingRepository:
    """SQLite repository for `LybrateListing`. Single-threaded use only."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self.init_schema()

    # ── lifecycle ────────────────────────────────────────────────────

    def __enter__(self) -> "LybrateListingRepository":
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
            "SELECT 1 FROM lybrate_listings WHERE profile_url = ? LIMIT 1",
            (profile_url,),
        )
        return cur.fetchone() is not None

    def exists_many(self, profile_urls: Iterable[str]) -> set[str]:
        urls = list(profile_urls)
        if not urls:
            return set()
        placeholders = ",".join("?" * len(urls))
        cur = self._conn.execute(
            f"SELECT profile_url FROM lybrate_listings "
            f"WHERE profile_url IN ({placeholders})",
            urls,
        )
        return {row[0] for row in cur.fetchall()}

    def get_by_url(self, profile_url: str) -> LybrateListing | None:
        cur = self._conn.execute(
            "SELECT * FROM lybrate_listings WHERE profile_url = ?",
            (profile_url,),
        )
        row = cur.fetchone()
        return _row_to_listing(row) if row else None

    def get_for_city(self, city: str) -> list[LybrateListing]:
        cur = self._conn.execute(
            "SELECT * FROM lybrate_listings WHERE city = ? "
            "ORDER BY discovered_at ASC",
            (city,),
        )
        return [_row_to_listing(row) for row in cur.fetchall()]

    def get_unsynced_for_city(self, city: str) -> list[LybrateListing]:
        cur = self._conn.execute(
            """
            SELECT * FROM lybrate_listings
            WHERE city = ?
              AND (last_synced_at IS NULL OR last_modified_at > last_synced_at)
            ORDER BY discovered_at ASC
            """,
            (city,),
        )
        return [_row_to_listing(row) for row in cur.fetchall()]

    def count_for_city(self, city: str) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM lybrate_listings WHERE city = ?", (city,),
        )
        return int(cur.fetchone()[0])

    def count_unsynced_for_city(self, city: str) -> int:
        cur = self._conn.execute(
            """
            SELECT COUNT(*) FROM lybrate_listings
            WHERE city = ?
              AND (last_synced_at IS NULL OR last_modified_at > last_synced_at)
            """,
            (city,),
        )
        return int(cur.fetchone()[0])

    # ── writes ───────────────────────────────────────────────────────

    def upsert_many(self, listings: Iterable[LybrateListing]) -> None:
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
                "UPDATE lybrate_listings SET last_synced_at = ? "
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
        f"INSERT INTO lybrate_listings ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(profile_url) DO UPDATE SET {update_clause}"
    )


def _listing_to_row(l: LybrateListing) -> dict[str, Any]:
    return {
        "profile_url": l.profile_url,
        "city": l.city,
        "doctor_name": l.doctor_name,
        "clinic_name": l.clinic_name,
        "address": l.address,
        "locality": l.locality,
        "postal_code": l.postal_code,
        "lat": l.lat,
        "lng": l.lng,
        "phone": l.phone,
        "specialty": l.specialty,
        "raw_json": json.dumps(l.raw_json),
        "discovered_at": l.discovered_at.isoformat(),
        "last_modified_at": l.last_modified_at.isoformat(),
        "last_synced_at": (
            l.last_synced_at.isoformat() if l.last_synced_at else None
        ),
    }


def _row_to_listing(row: sqlite3.Row) -> LybrateListing:
    return LybrateListing(
        profile_url=row["profile_url"],
        city=row["city"],
        doctor_name=row["doctor_name"],
        clinic_name=row["clinic_name"],
        address=row["address"],
        locality=row["locality"],
        postal_code=row["postal_code"],
        lat=row["lat"],
        lng=row["lng"],
        phone=row["phone"],
        specialty=row["specialty"],
        raw_json=json.loads(row["raw_json"]) if row["raw_json"] else {},
        discovered_at=datetime.fromisoformat(row["discovered_at"]),
        last_modified_at=datetime.fromisoformat(row["last_modified_at"]),
        last_synced_at=(
            datetime.fromisoformat(row["last_synced_at"])
            if row["last_synced_at"] else None
        ),
    )
