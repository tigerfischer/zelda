"""SQLite-backed persistence for `LeadEnrichment`.

One row per lead. Passes write their slice of signals via `upsert()`,
which merges new values with whatever is already stored — fields that
are still `None` are left alone so later passes don't clobber earlier
ones.

The `passes_completed`, `negative_theme_flags`, `service_mix`,
`equipment_claims`, `signal_extras` fields are JSON columns.
Everything else is a native SQLite type.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from zelda.models.lead_enrichment import LeadEnrichment


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS lead_enrichments (
    lead_id                         TEXT PRIMARY KEY,
    city                            TEXT NOT NULL,
    clinic_name                     TEXT,           -- copied from Lead.name by Pass 0

    -- reputation (Pass 0)
    google_review_count             INTEGER,
    google_rating                   REAL,
    gbp_has_hours                   INTEGER,
    gbp_photos_count                INTEGER,
    gbp_has_description             INTEGER,
    is_not_operational              INTEGER,

    -- reputation (Pass 1 — full review history)
    review_velocity_30d             INTEGER,
    review_velocity_90d             INTEGER,
    review_velocity_180d            INTEGER,
    owner_response_rate             REAL,
    owner_avg_response_days         REAL,
    has_revenue_leak_signal         INTEGER,
    negative_theme_flags            TEXT    NOT NULL DEFAULT '[]',

    -- reputation (Pass 3 — Practo)
    practo_review_count             INTEGER,
    practo_rating                   REAL,

    -- acquisition (Pass 0)
    has_website                     INTEGER,
    on_practo                       INTEGER,
    on_lybrate                      INTEGER,
    source_count                    INTEGER,
    nap_consistent                  INTEGER,
    is_chain                        INTEGER,
    is_hospital_embedded            INTEGER,

    -- acquisition (Pass 2 — website)
    website_loads                   INTEGER,
    website_is_mobile_friendly      INTEGER,
    website_has_schema_markup       INTEGER,
    website_has_blog                INTEGER,
    website_agency_credit           TEXT,

    -- conversion (Pass 2 — website)
    has_whatsapp_link               INTEGER,
    has_online_booking              INTEGER,
    has_chat_widget                 INTEGER,

    -- conversion (Pass 3 — Practo)
    practo_booking_enabled          INTEGER,

    -- ability to pay (Pass 3)
    practo_consultation_fee_inr     INTEGER,
    service_mix                     TEXT    NOT NULL DEFAULT '[]',
    equipment_claims                TEXT    NOT NULL DEFAULT '[]',
    years_in_operation              INTEGER,
    dentist_count                   INTEGER,

    -- owner / outreach (Pass 0 + 3)
    owner_name                      TEXT,
    owner_qualifications            TEXT,
    direct_phone                    TEXT,

    -- composite score (Pass 5)
    need_score                      INTEGER,
    score_tier                      TEXT,
    pitch_angle                     TEXT,

    -- metadata
    passes_completed                TEXT    NOT NULL DEFAULT '{}',
    signal_extras                   TEXT    NOT NULL DEFAULT '{}',
    enrichment_version              TEXT    NOT NULL DEFAULT '1',
    updated_at                      TEXT
);

CREATE INDEX IF NOT EXISTS idx_lead_enrichments_city
    ON lead_enrichments(city);
CREATE INDEX IF NOT EXISTS idx_lead_enrichments_tier
    ON lead_enrichments(city, score_tier);
CREATE INDEX IF NOT EXISTS idx_lead_enrichments_score
    ON lead_enrichments(city, need_score DESC);
"""


class LeadEnrichmentRepository:
    """Single-threaded SQLite repository for LeadEnrichment."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self.init_schema()

    def __enter__(self) -> "LeadEnrichmentRepository":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA_SQL)

    # ── reads ─────────────────────────────────────────────────────────

    def get(self, lead_id: str) -> LeadEnrichment | None:
        row = self._conn.execute(
            "SELECT * FROM lead_enrichments WHERE lead_id = ?", (lead_id,)
        ).fetchone()
        return _row_to_enrichment(row) if row else None

    def get_for_city(
        self,
        city: str,
        *,
        min_score: int | None = None,
        tier: str | None = None,
    ) -> list[LeadEnrichment]:
        q = "SELECT * FROM lead_enrichments WHERE city = ?"
        params: list[Any] = [city]
        if min_score is not None:
            q += " AND need_score >= ?"
            params.append(min_score)
        if tier:
            q += " AND score_tier = ?"
            params.append(tier)
        q += " ORDER BY need_score DESC NULLS LAST, google_review_count ASC NULLS LAST"
        rows = self._conn.execute(q, params).fetchall()
        return [_row_to_enrichment(r) for r in rows]

    def get_or_create(self, lead_id: str, *, city: str) -> LeadEnrichment:
        existing = self.get(lead_id)
        if existing is not None:
            return existing
        fresh = LeadEnrichment(lead_id=lead_id, city=city)
        self._insert(fresh)
        return fresh

    def count_for_city(self, city: str) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM lead_enrichments WHERE city = ?", (city,)
        ).fetchone()[0])

    def count_with_pass(self, city: str, pass_name: str) -> int:
        """Count enrichments where pass_name has been recorded."""
        rows = self._conn.execute(
            "SELECT passes_completed FROM lead_enrichments WHERE city = ?", (city,)
        ).fetchall()
        return sum(
            1 for r in rows
            if pass_name in json.loads(r["passes_completed"])
        )

    # ── writes ────────────────────────────────────────────────────────

    def upsert(self, enrichment: LeadEnrichment) -> None:
        """Insert or fully replace the enrichment record."""
        row = _enrichment_to_row(enrichment)
        cols = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row.keys())
        with self._conn:
            self._conn.execute(
                f"INSERT OR REPLACE INTO lead_enrichments ({cols}) VALUES ({placeholders})",
                row,
            )

    # ── private ───────────────────────────────────────────────────────

    def _insert(self, enrichment: LeadEnrichment) -> None:
        row = _enrichment_to_row(enrichment)
        cols = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row.keys())
        with self._conn:
            self._conn.execute(
                f"INSERT OR IGNORE INTO lead_enrichments ({cols}) VALUES ({placeholders})",
                row,
            )


def _bool_to_int(v: bool | None) -> int | None:
    return None if v is None else int(v)


def _int_to_bool(v: int | None) -> bool | None:
    return None if v is None else bool(v)


def _enrichment_to_row(e: LeadEnrichment) -> dict[str, Any]:
    return {
        "lead_id": e.lead_id,
        "city": e.city,
        "clinic_name": e.clinic_name,
        "google_review_count": e.google_review_count,
        "google_rating": e.google_rating,
        "gbp_has_hours": _bool_to_int(e.gbp_has_hours),
        "gbp_photos_count": e.gbp_photos_count,
        "gbp_has_description": _bool_to_int(e.gbp_has_description),
        "is_not_operational": _bool_to_int(e.is_not_operational),
        "review_velocity_30d": e.review_velocity_30d,
        "review_velocity_90d": e.review_velocity_90d,
        "review_velocity_180d": e.review_velocity_180d,
        "owner_response_rate": e.owner_response_rate,
        "owner_avg_response_days": e.owner_avg_response_days,
        "has_revenue_leak_signal": _bool_to_int(e.has_revenue_leak_signal),
        "negative_theme_flags": json.dumps(e.negative_theme_flags),
        "practo_review_count": e.practo_review_count,
        "practo_rating": e.practo_rating,
        "has_website": _bool_to_int(e.has_website),
        "on_practo": _bool_to_int(e.on_practo),
        "on_lybrate": _bool_to_int(e.on_lybrate),
        "source_count": e.source_count,
        "nap_consistent": _bool_to_int(e.nap_consistent),
        "is_chain": _bool_to_int(e.is_chain),
        "is_hospital_embedded": _bool_to_int(e.is_hospital_embedded),
        "website_loads": _bool_to_int(e.website_loads),
        "website_is_mobile_friendly": _bool_to_int(e.website_is_mobile_friendly),
        "website_has_schema_markup": _bool_to_int(e.website_has_schema_markup),
        "website_has_blog": _bool_to_int(e.website_has_blog),
        "website_agency_credit": e.website_agency_credit,
        "has_whatsapp_link": _bool_to_int(e.has_whatsapp_link),
        "has_online_booking": _bool_to_int(e.has_online_booking),
        "has_chat_widget": _bool_to_int(e.has_chat_widget),
        "practo_booking_enabled": _bool_to_int(e.practo_booking_enabled),
        "practo_consultation_fee_inr": e.practo_consultation_fee_inr,
        "service_mix": json.dumps(e.service_mix),
        "equipment_claims": json.dumps(e.equipment_claims),
        "years_in_operation": e.years_in_operation,
        "dentist_count": e.dentist_count,
        "owner_name": e.owner_name,
        "owner_qualifications": e.owner_qualifications,
        "direct_phone": e.direct_phone,
        "need_score": e.need_score,
        "score_tier": e.score_tier,
        "pitch_angle": e.pitch_angle,
        "passes_completed": json.dumps(e.passes_completed),
        "signal_extras": json.dumps(e.signal_extras),
        "enrichment_version": e.enrichment_version,
        "updated_at": e.updated_at.isoformat() if e.updated_at else None,
    }


def _row_to_enrichment(row: sqlite3.Row) -> LeadEnrichment:
    return LeadEnrichment(
        lead_id=row["lead_id"],
        city=row["city"],
        clinic_name=row["clinic_name"],
        google_review_count=row["google_review_count"],
        google_rating=row["google_rating"],
        gbp_has_hours=_int_to_bool(row["gbp_has_hours"]),
        gbp_photos_count=row["gbp_photos_count"],
        gbp_has_description=_int_to_bool(row["gbp_has_description"]),
        is_not_operational=_int_to_bool(row["is_not_operational"]),
        review_velocity_30d=row["review_velocity_30d"],
        review_velocity_90d=row["review_velocity_90d"],
        review_velocity_180d=row["review_velocity_180d"],
        owner_response_rate=row["owner_response_rate"],
        owner_avg_response_days=row["owner_avg_response_days"],
        has_revenue_leak_signal=_int_to_bool(row["has_revenue_leak_signal"]),
        negative_theme_flags=json.loads(row["negative_theme_flags"]),
        practo_review_count=row["practo_review_count"],
        practo_rating=row["practo_rating"],
        has_website=_int_to_bool(row["has_website"]),
        on_practo=_int_to_bool(row["on_practo"]),
        on_lybrate=_int_to_bool(row["on_lybrate"]),
        source_count=row["source_count"],
        nap_consistent=_int_to_bool(row["nap_consistent"]),
        is_chain=_int_to_bool(row["is_chain"]),
        is_hospital_embedded=_int_to_bool(row["is_hospital_embedded"]),
        website_loads=_int_to_bool(row["website_loads"]),
        website_is_mobile_friendly=_int_to_bool(row["website_is_mobile_friendly"]),
        website_has_schema_markup=_int_to_bool(row["website_has_schema_markup"]),
        website_has_blog=_int_to_bool(row["website_has_blog"]),
        website_agency_credit=row["website_agency_credit"],
        has_whatsapp_link=_int_to_bool(row["has_whatsapp_link"]),
        has_online_booking=_int_to_bool(row["has_online_booking"]),
        has_chat_widget=_int_to_bool(row["has_chat_widget"]),
        practo_booking_enabled=_int_to_bool(row["practo_booking_enabled"]),
        practo_consultation_fee_inr=row["practo_consultation_fee_inr"],
        service_mix=json.loads(row["service_mix"]),
        equipment_claims=json.loads(row["equipment_claims"]),
        years_in_operation=row["years_in_operation"],
        dentist_count=row["dentist_count"],
        owner_name=row["owner_name"],
        owner_qualifications=row["owner_qualifications"],
        direct_phone=row["direct_phone"],
        need_score=row["need_score"],
        score_tier=row["score_tier"],
        pitch_angle=row["pitch_angle"],
        passes_completed=json.loads(row["passes_completed"]),
        signal_extras=json.loads(row["signal_extras"]),
        enrichment_version=row["enrichment_version"],
        updated_at=(
            datetime.fromisoformat(row["updated_at"])
            if row["updated_at"] else None
        ),
    )


__all__ = ["LeadEnrichmentRepository"]
