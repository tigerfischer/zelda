"""SQLite-backed persistence for `PractoProfile`.

One row per `place_id` — the same natural key as `raw_leads`. The two
tables are intentionally NOT joined via FK constraints: an enrichment
row can outlive the source lead (e.g. business closes) and we don't
want a cascade delete to silently drop scraped history.

Row lifecycle
-------------
- Operator (or a future discovery step) creates a *stub* row with
  `upsert_stub(place_id, practo_url)`. Status='pending', fetched_at=None.
- Enrichment controller polls `get_pending(...)` (or `get_stale(...)` for
  refresh), runs the Apify actor, and calls `upsert(profile)` with the
  fully-populated PractoProfile (status='ok' / 'not_found' / 'error').
- `discovered_at` is preserved across upserts; `last_modified_at` bumps
  every write. Same convention as `RawLeadRepository`.

JSON columns
------------
Lists / dicts are stored as JSON-encoded TEXT (no JSON1 extension
needed — we always read/write whole values).
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from zelda.models.practo_profile import PractoFetchStatus, PractoProfile


_COLUMNS: tuple[str, ...] = (
    "place_id",
    "practo_url",
    "practo_doctor_id",
    "profile_url",
    "name",
    "qualifications",
    "experience_years",
    "specializations",
    "languages",
    "registrations",
    "education",
    "awards",
    "memberships",
    "clinic_name",
    "clinic_address",
    "clinic_locality",
    "clinic_city",
    "consultation_fee",
    "consultation_fee_currency",
    "services",
    "operating_hours",
    "lat",
    "lng",
    "recommendation_percent",
    "rating",
    "reviews_count",
    "patient_count",
    "has_practo_plus_badge",
    "next_available_at",
    "profile_image_url",
    "photo_urls",
    "summary",
    "fetch_status",
    "error_message",
    "fetched_at",
    "raw_json",
    "discovered_at",
    "last_modified_at",
)

_UPSERT_IMMUTABLE: frozenset[str] = frozenset({
    "place_id",       # natural key
    "discovered_at",  # immutable per row
})

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS practo_profiles (
    place_id                  TEXT PRIMARY KEY,
    practo_url                TEXT NOT NULL,

    practo_doctor_id          TEXT,
    profile_url               TEXT,
    name                      TEXT,

    qualifications            TEXT NOT NULL DEFAULT '[]',
    experience_years          INTEGER,
    specializations           TEXT NOT NULL DEFAULT '[]',
    languages                 TEXT NOT NULL DEFAULT '[]',
    registrations             TEXT NOT NULL DEFAULT '[]',
    education                 TEXT NOT NULL DEFAULT '[]',
    awards                    TEXT NOT NULL DEFAULT '[]',
    memberships               TEXT NOT NULL DEFAULT '[]',

    clinic_name               TEXT,
    clinic_address            TEXT,
    clinic_locality           TEXT,
    clinic_city               TEXT,

    consultation_fee          INTEGER,
    consultation_fee_currency TEXT,
    services                  TEXT NOT NULL DEFAULT '[]',
    operating_hours           TEXT,

    lat                       REAL,
    lng                       REAL,

    recommendation_percent    INTEGER,
    rating                    REAL,
    reviews_count             INTEGER,
    patient_count             INTEGER,
    has_practo_plus_badge     INTEGER,
    next_available_at         TEXT,

    profile_image_url         TEXT,
    photo_urls                TEXT NOT NULL DEFAULT '[]',

    summary                   TEXT,

    fetch_status              TEXT NOT NULL DEFAULT 'pending',
    error_message             TEXT,
    fetched_at                TEXT,

    raw_json                  TEXT NOT NULL DEFAULT '{}',

    discovered_at             TEXT NOT NULL,
    last_modified_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_practo_status
    ON practo_profiles(fetch_status, fetched_at);
"""


class PractoProfileRepository:
    """SQLite repository for `PractoProfile`. Single-threaded use only."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self.init_schema()

    # ── lifecycle ────────────────────────────────────────────────────

    def __enter__(self) -> "PractoProfileRepository":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA_SQL)

    # ── reads ────────────────────────────────────────────────────────

    def get_by_place_id(self, place_id: str) -> PractoProfile | None:
        cur = self._conn.execute(
            "SELECT * FROM practo_profiles WHERE place_id = ?",
            (place_id,),
        )
        row = cur.fetchone()
        return _row_to_profile(row) if row else None

    def get_pending(self, *, limit: int | None = None) -> list[PractoProfile]:
        """Stub rows that have never been fetched. Ordered by
        discovery time so older stubs get processed first."""
        sql = (
            "SELECT * FROM practo_profiles "
            "WHERE fetch_status = 'pending' "
            "ORDER BY discovered_at ASC"
        )
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (int(limit),)
        cur = self._conn.execute(sql, params)
        return [_row_to_profile(r) for r in cur.fetchall()]

    def get_stale(
        self,
        *,
        older_than: timedelta,
        now: datetime | None = None,
        statuses: tuple[PractoFetchStatus, ...] = ("ok",),
        limit: int | None = None,
    ) -> list[PractoProfile]:
        """Rows whose last successful fetch is older than `older_than`.

        Default refresh policy: only refresh rows that previously
        fetched successfully (`status='ok'`). Errored rows are NOT
        included by default — they need explicit operator action so
        we don't burn Apify credits on a permanently broken URL.
        """
        if not statuses:
            return []
        anchor = (now or datetime.now(timezone.utc)) - older_than
        placeholders = ",".join("?" * len(statuses))
        sql = (
            f"SELECT * FROM practo_profiles "
            f"WHERE fetch_status IN ({placeholders}) "
            f"  AND fetched_at IS NOT NULL "
            f"  AND fetched_at < ? "
            f"ORDER BY fetched_at ASC"
        )
        params: list[Any] = [*statuses, anchor.isoformat()]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        cur = self._conn.execute(sql, params)
        return [_row_to_profile(r) for r in cur.fetchall()]

    def count_by_status(self) -> dict[str, int]:
        cur = self._conn.execute(
            "SELECT fetch_status, COUNT(*) AS n "
            "FROM practo_profiles GROUP BY fetch_status"
        )
        return {row["fetch_status"]: row["n"] for row in cur.fetchall()}

    # ── writes ───────────────────────────────────────────────────────

    def upsert_stub(
        self,
        place_id: str,
        practo_url: str,
        *,
        now: datetime | None = None,
    ) -> PractoProfile:
        """Create or update a stub row binding a Practo URL to a place.

        Idempotent: re-calling with the same (place_id, practo_url)
        bumps `last_modified_at` but does not reset the fetch state.
        Calling with a NEW practo_url for an existing place_id rewrites
        the URL but also resets fetch_status to 'pending' (because the
        old scraped data is now bound to a stale URL).
        """
        ts = now or datetime.now(timezone.utc)
        existing = self.get_by_place_id(place_id)

        if existing is None:
            stub = PractoProfile(
                place_id=place_id,
                practo_url=practo_url,
                fetch_status="pending",
                discovered_at=ts,
                last_modified_at=ts,
            )
            self._upsert_one(stub)
            return stub

        if existing.practo_url == practo_url:
            # No-op for state, but still bump last_modified_at to mark touch.
            existing.last_modified_at = ts
            self._upsert_one(existing)
            return existing

        # URL changed — old scraped data is now stale; reset to pending.
        reset = PractoProfile(
            place_id=place_id,
            practo_url=practo_url,
            fetch_status="pending",
            discovered_at=existing.discovered_at,
            last_modified_at=ts,
        )
        self._upsert_one(reset)
        return reset

    def upsert(
        self,
        profile: PractoProfile,
        *,
        now: datetime | None = None,
    ) -> None:
        """Upsert a fully-formed profile (typically the result of an
        enrichment run). Bumps last_modified_at; preserves discovered_at."""
        ts = now or datetime.now(timezone.utc)
        profile.last_modified_at = ts
        self._upsert_one(profile)

    def upsert_many(self, profiles: Iterable[PractoProfile]) -> None:
        rows = [_profile_to_row(p) for p in profiles]
        if not rows:
            return
        with self._conn:
            self._conn.executemany(_upsert_sql(), rows)

    def _upsert_one(self, profile: PractoProfile) -> None:
        with self._conn:
            self._conn.execute(_upsert_sql(), _profile_to_row(profile))


# ── module-private helpers ───────────────────────────────────────────


def _upsert_sql() -> str:
    cols = ", ".join(_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in _COLUMNS)
    update_cols = [c for c in _COLUMNS if c not in _UPSERT_IMMUTABLE]
    update_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
    return (
        f"INSERT INTO practo_profiles ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(place_id) DO UPDATE SET {update_clause}"
    )


def _profile_to_row(p: PractoProfile) -> dict[str, Any]:
    return {
        "place_id": p.place_id,
        "practo_url": p.practo_url,
        "practo_doctor_id": p.practo_doctor_id,
        "profile_url": p.profile_url,
        "name": p.name,
        "qualifications": json.dumps(p.qualifications),
        "experience_years": p.experience_years,
        "specializations": json.dumps(p.specializations),
        "languages": json.dumps(p.languages),
        "registrations": json.dumps(p.registrations),
        "education": json.dumps(p.education),
        "awards": json.dumps(p.awards),
        "memberships": json.dumps(p.memberships),
        "clinic_name": p.clinic_name,
        "clinic_address": p.clinic_address,
        "clinic_locality": p.clinic_locality,
        "clinic_city": p.clinic_city,
        "consultation_fee": p.consultation_fee,
        "consultation_fee_currency": p.consultation_fee_currency,
        "services": json.dumps(p.services),
        "operating_hours": (
            json.dumps(p.operating_hours) if p.operating_hours is not None else None
        ),
        "lat": p.lat,
        "lng": p.lng,
        "recommendation_percent": p.recommendation_percent,
        "rating": p.rating,
        "reviews_count": p.reviews_count,
        "patient_count": p.patient_count,
        "has_practo_plus_badge": (
            None if p.has_practo_plus_badge is None
            else int(p.has_practo_plus_badge)
        ),
        "next_available_at": (
            p.next_available_at.isoformat() if p.next_available_at else None
        ),
        "profile_image_url": p.profile_image_url,
        "photo_urls": json.dumps(p.photo_urls),
        "summary": p.summary,
        "fetch_status": p.fetch_status,
        "error_message": p.error_message,
        "fetched_at": p.fetched_at.isoformat() if p.fetched_at else None,
        "raw_json": json.dumps(p.raw_json),
        "discovered_at": p.discovered_at.isoformat(),
        "last_modified_at": p.last_modified_at.isoformat(),
    }


def _row_to_profile(row: sqlite3.Row) -> PractoProfile:
    return PractoProfile(
        place_id=row["place_id"],
        practo_url=row["practo_url"],
        practo_doctor_id=row["practo_doctor_id"],
        profile_url=row["profile_url"],
        name=row["name"],
        qualifications=_from_list(row["qualifications"]),
        experience_years=row["experience_years"],
        specializations=_from_list(row["specializations"]),
        languages=_from_list(row["languages"]),
        registrations=_from_list(row["registrations"]),
        education=_from_list(row["education"]),
        awards=_from_list(row["awards"]),
        memberships=_from_list(row["memberships"]),
        clinic_name=row["clinic_name"],
        clinic_address=row["clinic_address"],
        clinic_locality=row["clinic_locality"],
        clinic_city=row["clinic_city"],
        consultation_fee=row["consultation_fee"],
        consultation_fee_currency=row["consultation_fee_currency"],
        services=_from_list(row["services"]),
        operating_hours=(
            json.loads(row["operating_hours"]) if row["operating_hours"] else None
        ),
        lat=row["lat"],
        lng=row["lng"],
        recommendation_percent=row["recommendation_percent"],
        rating=row["rating"],
        reviews_count=row["reviews_count"],
        patient_count=row["patient_count"],
        has_practo_plus_badge=(
            None if row["has_practo_plus_badge"] is None
            else bool(row["has_practo_plus_badge"])
        ),
        next_available_at=(
            datetime.fromisoformat(row["next_available_at"])
            if row["next_available_at"] else None
        ),
        profile_image_url=row["profile_image_url"],
        photo_urls=_from_list(row["photo_urls"]),
        summary=row["summary"],
        fetch_status=row["fetch_status"],
        error_message=row["error_message"],
        fetched_at=(
            datetime.fromisoformat(row["fetched_at"]) if row["fetched_at"] else None
        ),
        raw_json=json.loads(row["raw_json"]) if row["raw_json"] else {},
        discovered_at=datetime.fromisoformat(row["discovered_at"]),
        last_modified_at=datetime.fromisoformat(row["last_modified_at"]),
    )


def _from_list(value: str | None) -> list[Any]:
    if not value:
        return []
    out = json.loads(value)
    return out if isinstance(out, list) else []
