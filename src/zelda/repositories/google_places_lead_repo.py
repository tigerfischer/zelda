"""SQLite-backed persistence for `GooglePlacesLead`.

This is the *system of record*. Drive is a one-way projection of what
lives here; controllers above never persist to Drive directly.

Schema design
-------------
- One row per `place_id` (the natural unique key from Google Places).
- JSON-typed fields (`reviews`, `types`, `extras`, `raw_json`, etc.)
  are stored as TEXT containing JSON. SQLite has a JSON1 extension but
  we don't need it: we always read/write whole values.
- Datetimes are stored as ISO 8601 TEXT. SQLite has no native datetime;
  this keeps comparisons string-orderable and tooling-friendly.
- `discovered_at` is immutable per row; `last_modified_at` is bumped on
  every upsert; `last_synced_at` is owned by the sync controller and
  is never touched by `upsert_many`.

The delta-detection contract powering sync is:
    last_synced_at IS NULL OR last_modified_at > last_synced_at
which `get_unsynced_for_city` and `count_unsynced_for_city` return.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from zelda.db import connect as _db_connect
from zelda.models.google_places_lead import GooglePlacesLead


_COLUMNS: tuple[str, ...] = (
    "place_id",
    "city",
    "name",
    "formatted_address",
    "short_address",
    "address_components",
    "lat",
    "lng",
    "phone",
    "phone_intl",
    "website",
    "google_maps_url",
    "rating",
    "review_count",
    "reviews",
    "business_status",
    "primary_type",
    "types",
    "price_level",
    "editorial_summary",
    "photos_count",
    "opening_hours",
    "extras",
    "raw_json",
    "discovered_at",
    "last_modified_at",
    "last_synced_at",
)

# Columns NOT updated on conflict — these are immutable per-row or
# owned by another controller.
_UPSERT_IMMUTABLE: frozenset[str] = frozenset({
    "place_id",         # natural key
    "discovered_at",    # immutable per row
    "last_synced_at",   # owned by sync controller
})

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS google_places_leads (
    place_id            TEXT PRIMARY KEY,
    city                TEXT NOT NULL,
    name                TEXT NOT NULL,

    formatted_address   TEXT,
    short_address       TEXT,
    address_components  TEXT,
    lat                 REAL,
    lng                 REAL,

    phone               TEXT,
    phone_intl          TEXT,
    website             TEXT,
    google_maps_url     TEXT,

    rating              REAL,
    review_count        INTEGER,
    reviews             TEXT,

    business_status     TEXT,
    primary_type        TEXT,
    types               TEXT,
    price_level         TEXT,
    editorial_summary   TEXT,
    photos_count        INTEGER,
    opening_hours       TEXT,

    extras              TEXT NOT NULL DEFAULT '{}',
    raw_json            TEXT NOT NULL DEFAULT '{}',

    discovered_at       TEXT NOT NULL,
    last_modified_at    TEXT NOT NULL,
    last_synced_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_google_places_leads_city ON google_places_leads(city);
CREATE INDEX IF NOT EXISTS idx_google_places_leads_sync
    ON google_places_leads(city, last_modified_at, last_synced_at);
"""


class GooglePlacesLeadRepository:
    """SQLite repository for `GooglePlacesLead`. Single-threaded use only."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = _db_connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self.init_schema()

    # ── lifecycle ────────────────────────────────────────────────────

    def __enter__(self) -> "GooglePlacesLeadRepository":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def init_schema(self) -> None:
        """Idempotent schema creation."""
        with self._conn:
            self._conn.executescript(_SCHEMA_SQL)

    # ── reads ────────────────────────────────────────────────────────

    def exists(self, place_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM google_places_leads WHERE place_id = ? LIMIT 1",
            (place_id,),
        )
        return cur.fetchone() is not None

    def exists_many(self, place_ids: Iterable[str]) -> set[str]:
        """Return the subset of `place_ids` that already exist in the DB.

        Useful when the discover controller wants to filter a deduped
        candidate list down to only-new place_ids in a single SQL round
        trip (instead of N `exists()` calls).
        """
        ids = list(place_ids)
        if not ids:
            return set()
        placeholders = ",".join("?" * len(ids))
        cur = self._conn.execute(
            f"SELECT place_id FROM google_places_leads WHERE place_id IN ({placeholders})",
            ids,
        )
        return {row[0] for row in cur.fetchall()}

    def get_by_id(self, place_id: str) -> GooglePlacesLead | None:
        cur = self._conn.execute(
            "SELECT * FROM google_places_leads WHERE place_id = ?",
            (place_id,),
        )
        row = cur.fetchone()
        return _row_to_lead(row) if row else None

    def get_for_city(self, city: str) -> list[GooglePlacesLead]:
        cur = self._conn.execute(
            "SELECT * FROM google_places_leads WHERE city = ? ORDER BY discovered_at ASC",
            (city,),
        )
        return [_row_to_lead(row) for row in cur.fetchall()]

    def get_unsynced_for_city(self, city: str) -> list[GooglePlacesLead]:
        """Rows in `city` whose `last_modified_at` is newer than their
        `last_synced_at` (or never synced). The delta the sync
        controller will push to Drive."""
        cur = self._conn.execute(
            """
            SELECT * FROM google_places_leads
            WHERE city = ?
              AND (last_synced_at IS NULL OR last_modified_at > last_synced_at)
            ORDER BY discovered_at ASC
            """,
            (city,),
        )
        return [_row_to_lead(row) for row in cur.fetchall()]

    def count_for_city(self, city: str) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM google_places_leads WHERE city = ?", (city,)
        )
        return int(cur.fetchone()[0])

    def count_unsynced_for_city(self, city: str) -> int:
        cur = self._conn.execute(
            """
            SELECT COUNT(*) FROM google_places_leads
            WHERE city = ?
              AND (last_synced_at IS NULL OR last_modified_at > last_synced_at)
            """,
            (city,),
        )
        return int(cur.fetchone()[0])

    # ── writes ───────────────────────────────────────────────────────

    def upsert_many(self, leads: Iterable[GooglePlacesLead]) -> None:
        """Insert new rows; for existing place_ids, update mutable fields
        but preserve `discovered_at` and `last_synced_at`."""
        rows = [_lead_to_row(lead) for lead in leads]
        if not rows:
            return
        with self._conn:
            self._conn.executemany(_upsert_sql(), rows)

    def mark_synced(
        self,
        place_ids: Iterable[str],
        *,
        synced_at: datetime | None = None,
    ) -> None:
        """Stamp `last_synced_at` on each row. Default = current UTC time."""
        ids = list(place_ids)
        if not ids:
            return
        ts = (synced_at or datetime.now(timezone.utc)).isoformat()
        with self._conn:
            self._conn.executemany(
                "UPDATE google_places_leads SET last_synced_at = ? WHERE place_id = ?",
                [(ts, pid) for pid in ids],
            )


# ── module-private helpers ───────────────────────────────────────────


def _upsert_sql() -> str:
    cols = ", ".join(_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in _COLUMNS)
    update_cols = [c for c in _COLUMNS if c not in _UPSERT_IMMUTABLE]
    update_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
    return (
        f"INSERT INTO google_places_leads ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(place_id) DO UPDATE SET {update_clause}"
    )


def _lead_to_row(lead: GooglePlacesLead) -> dict[str, Any]:
    """GooglePlacesLead → dict suitable for executemany with named placeholders."""
    return {
        "place_id": lead.place_id,
        "city": lead.city,
        "name": lead.name,
        "formatted_address": lead.formatted_address,
        "short_address": lead.short_address,
        "address_components": _to_json(lead.address_components),
        "lat": lead.lat,
        "lng": lead.lng,
        "phone": lead.phone,
        "phone_intl": lead.phone_intl,
        "website": lead.website,
        "google_maps_url": lead.google_maps_url,
        "rating": lead.rating,
        "review_count": lead.review_count,
        "reviews": _to_json(lead.reviews),
        "business_status": lead.business_status,
        "primary_type": lead.primary_type,
        "types": _to_json(lead.types),
        "price_level": lead.price_level,
        "editorial_summary": lead.editorial_summary,
        "photos_count": lead.photos_count,
        "opening_hours": _to_json(lead.opening_hours),
        "extras": json.dumps(lead.extras),
        "raw_json": json.dumps(lead.raw_json),
        "discovered_at": lead.discovered_at.isoformat(),
        "last_modified_at": lead.last_modified_at.isoformat(),
        "last_synced_at": lead.last_synced_at.isoformat() if lead.last_synced_at else None,
    }


def _row_to_lead(row: sqlite3.Row) -> GooglePlacesLead:
    return GooglePlacesLead(
        place_id=row["place_id"],
        city=row["city"],
        name=row["name"],
        formatted_address=row["formatted_address"],
        short_address=row["short_address"],
        address_components=_from_json(row["address_components"]),
        lat=row["lat"],
        lng=row["lng"],
        phone=row["phone"],
        phone_intl=row["phone_intl"],
        website=row["website"],
        google_maps_url=row["google_maps_url"],
        rating=row["rating"],
        review_count=row["review_count"],
        reviews=_from_json(row["reviews"]),
        business_status=row["business_status"],
        primary_type=row["primary_type"],
        types=_from_json(row["types"]),
        price_level=row["price_level"],
        editorial_summary=row["editorial_summary"],
        photos_count=row["photos_count"],
        opening_hours=_from_json(row["opening_hours"]),
        extras=json.loads(row["extras"]) if row["extras"] else {},
        raw_json=json.loads(row["raw_json"]) if row["raw_json"] else {},
        discovered_at=datetime.fromisoformat(row["discovered_at"]),
        last_modified_at=datetime.fromisoformat(row["last_modified_at"]),
        last_synced_at=(
            datetime.fromisoformat(row["last_synced_at"])
            if row["last_synced_at"]
            else None
        ),
    )


def _to_json(value: Any) -> str | None:
    return json.dumps(value) if value is not None else None


def _from_json(value: str | None) -> Any:
    return json.loads(value) if value else None
