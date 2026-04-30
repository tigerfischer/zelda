"""SQLite-backed persistence for `Lead` — the final output of matching.

Leads are written with a `run_id` so historical runs are preserved.
Queries default to the most recent run for a city. The `lybrate_urls`
field is a JSON list (one clinic may have N matched Lybrate doctor entries).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from zelda.models.lead import Lead


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS leads (
    lead_id             TEXT PRIMARY KEY,
    city                TEXT NOT NULL,
    run_id              TEXT NOT NULL,
    tier                TEXT NOT NULL CHECK(tier IN ('enriched', 'standalone')),
    name                TEXT NOT NULL,
    address             TEXT,
    lat                 REAL,
    lng                 REAL,
    phone               TEXT,
    website             TEXT,
    google_maps_url     TEXT,
    rating              REAL,
    review_count        INTEGER,
    google_places_id    TEXT,
    practo_url          TEXT,
    lybrate_urls        TEXT NOT NULL DEFAULT '[]',
    match_confidence    REAL,
    match_notes         TEXT,
    human_review_needed INTEGER NOT NULL DEFAULT 0,
    source_data         TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leads_city     ON leads(city);
CREATE INDEX IF NOT EXISTS idx_leads_run      ON leads(run_id);
CREATE INDEX IF NOT EXISTS idx_leads_city_run ON leads(city, run_id);
CREATE INDEX IF NOT EXISTS idx_leads_tier     ON leads(city, tier);
"""


class LeadRepository:
    """SQLite repository for `Lead`. Single-threaded use only."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self.init_schema()

    def __enter__(self) -> "LeadRepository":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA_SQL)

    # ── reads ────────────────────────────────────────────────────────

    def get_for_city(
        self,
        city: str,
        *,
        run_id: str | None = None,
        tier: str | None = None,
    ) -> list[Lead]:
        """Return leads for city. Defaults to most recent run_id."""
        rid = run_id or self._latest_run_id(city)
        if rid is None:
            return []
        q = "SELECT * FROM leads WHERE city=? AND run_id=?"
        params: list[Any] = [city, rid]
        if tier:
            q += " AND tier=?"
            params.append(tier)
        q += " ORDER BY name ASC"
        rows = self._conn.execute(q, params).fetchall()
        return [_row_to_lead(r) for r in rows]

    def count_for_city(self, city: str, *, run_id: str | None = None) -> int:
        rid = run_id or self._latest_run_id(city)
        if rid is None:
            return 0
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM leads WHERE city=? AND run_id=?",
            (city, rid),
        ).fetchone()[0])

    def list_run_ids(self, city: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT run_id FROM leads WHERE city=? ORDER BY run_id DESC",
            (city,),
        ).fetchall()
        return [r[0] for r in rows]

    # ── writes ───────────────────────────────────────────────────────

    def insert_many(self, leads: list[Lead]) -> None:
        if not leads:
            return
        rows = [_lead_to_row(l) for l in leads]
        with self._conn:
            self._conn.executemany(
                """
                INSERT OR IGNORE INTO leads
                    (lead_id, city, run_id, tier, name, address, lat, lng,
                     phone, website, google_maps_url, rating, review_count,
                     google_places_id, practo_url, lybrate_urls,
                     match_confidence, match_notes, human_review_needed,
                     source_data, created_at)
                VALUES
                    (:lead_id, :city, :run_id, :tier, :name, :address, :lat, :lng,
                     :phone, :website, :google_maps_url, :rating, :review_count,
                     :google_places_id, :practo_url, :lybrate_urls,
                     :match_confidence, :match_notes, :human_review_needed,
                     :source_data, :created_at)
                """,
                rows,
            )

    # ── private ──────────────────────────────────────────────────────

    def _latest_run_id(self, city: str) -> str | None:
        row = self._conn.execute(
            "SELECT run_id FROM leads WHERE city=? ORDER BY created_at DESC LIMIT 1",
            (city,),
        ).fetchone()
        return row[0] if row else None


def _lead_to_row(l: Lead) -> dict[str, Any]:
    return {
        "lead_id": l.lead_id,
        "city": l.city,
        "run_id": l.run_id,
        "tier": l.tier,
        "name": l.name,
        "address": l.address,
        "lat": l.lat,
        "lng": l.lng,
        "phone": l.phone,
        "website": l.website,
        "google_maps_url": l.google_maps_url,
        "rating": l.rating,
        "review_count": l.review_count,
        "google_places_id": l.google_places_id,
        "practo_url": l.practo_url,
        "lybrate_urls": json.dumps(l.lybrate_urls),
        "match_confidence": l.match_confidence,
        "match_notes": l.match_notes,
        "human_review_needed": int(l.human_review_needed),
        "source_data": json.dumps(l.source_data),
        "created_at": l.created_at.isoformat(),
    }


def _row_to_lead(row: sqlite3.Row) -> Lead:
    return Lead(
        lead_id=row["lead_id"],
        city=row["city"],
        run_id=row["run_id"],
        tier=row["tier"],
        name=row["name"],
        address=row["address"],
        lat=row["lat"],
        lng=row["lng"],
        phone=row["phone"],
        website=row["website"],
        google_maps_url=row["google_maps_url"],
        rating=row["rating"],
        review_count=row["review_count"],
        google_places_id=row["google_places_id"],
        practo_url=row["practo_url"],
        lybrate_urls=json.loads(row["lybrate_urls"]),
        match_confidence=row["match_confidence"],
        match_notes=row["match_notes"],
        human_review_needed=bool(row["human_review_needed"]),
        source_data=json.loads(row["source_data"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


__all__ = ["LeadRepository"]
