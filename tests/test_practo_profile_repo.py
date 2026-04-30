from datetime import datetime, timedelta, timezone

import pytest

from zelda.models.practo_profile import PractoProfile
from zelda.repositories.practo_profile_repo import PractoProfileRepository


_T1 = datetime(2026, 4, 29, 10, 0, 0, tzinfo=timezone.utc)
_T2 = _T1 + timedelta(hours=1)


@pytest.fixture
def repo():
    r = PractoProfileRepository(":memory:")
    yield r
    r.close()


def _enriched(place_id: str = "ChIJ_X", **overrides) -> PractoProfile:
    base = dict(
        place_id=place_id,
        practo_url=f"https://www.practo.com/bangalore/doctor/dr-{place_id.lower()}",
        name="Dr. K A Mohan",
        qualifications=["BDS", "MDS - Orthodontics"],
        experience_years=57,
        specializations=["Orthodontist", "Dental Surgeon"],
        languages=["English", "Hindi", "Kannada"],
        consultation_fee=500,
        consultation_fee_currency="INR",
        recommendation_percent=73,
        reviews_count=42,
        services=["Braces", "Cleaning"],
        photo_urls=["https://x.example/a.jpg", "https://x.example/b.jpg"],
        fetch_status="ok",
        fetched_at=_T1,
        raw_json={"some": "stuff"},
        discovered_at=_T1,
        last_modified_at=_T1,
    )
    base.update(overrides)
    return PractoProfile(**base)


# ── stub creation ────────────────────────────────────────────────────


def test_upsert_stub_creates_pending_row(repo):
    out = repo.upsert_stub("ChIJ_A", "https://www.practo.com/x", now=_T1)
    assert out.fetch_status == "pending"
    assert out.fetched_at is None
    assert out.discovered_at == _T1

    fetched = repo.get_by_place_id("ChIJ_A")
    assert fetched is not None
    assert fetched.fetch_status == "pending"


def test_upsert_stub_with_same_url_is_idempotent(repo):
    a = repo.upsert_stub("ChIJ_A", "https://x", now=_T1)
    b = repo.upsert_stub("ChIJ_A", "https://x", now=_T2)
    assert a.discovered_at == b.discovered_at == _T1
    # last_modified_at should bump on the touch
    assert b.last_modified_at == _T2


def test_upsert_stub_with_changed_url_resets_to_pending(repo):
    repo.upsert_stub("ChIJ_A", "https://old", now=_T1)
    repo.upsert(_enriched("ChIJ_A", practo_url="https://old"), now=_T1)

    after = repo.upsert_stub("ChIJ_A", "https://new", now=_T2)
    assert after.fetch_status == "pending"
    assert after.fetched_at is None
    assert after.discovered_at == _T1  # preserved
    assert after.last_modified_at == _T2

    persisted = repo.get_by_place_id("ChIJ_A")
    assert persisted is not None
    assert persisted.practo_url == "https://new"
    assert persisted.fetch_status == "pending"
    assert persisted.name is None  # old enriched data wiped
    assert persisted.consultation_fee is None


# ── enriched upsert ──────────────────────────────────────────────────


def test_upsert_enriched_round_trips_all_fields(repo):
    p = _enriched()
    repo.upsert(p)
    got = repo.get_by_place_id("ChIJ_X")
    assert got is not None
    assert got.name == "Dr. K A Mohan"
    assert got.qualifications == ["BDS", "MDS - Orthodontics"]
    assert got.experience_years == 57
    assert got.specializations == ["Orthodontist", "Dental Surgeon"]
    assert got.languages == ["English", "Hindi", "Kannada"]
    assert got.consultation_fee == 500
    assert got.consultation_fee_currency == "INR"
    assert got.recommendation_percent == 73
    assert got.reviews_count == 42
    assert got.services == ["Braces", "Cleaning"]
    assert got.photo_urls == ["https://x.example/a.jpg", "https://x.example/b.jpg"]
    assert got.fetch_status == "ok"
    assert got.fetched_at == _T1
    assert got.raw_json == {"some": "stuff"}


def test_upsert_preserves_discovered_at_on_update(repo):
    repo.upsert_stub("ChIJ_X", "https://x", now=_T1)
    repo.upsert(_enriched("ChIJ_X", discovered_at=_T2), now=_T2)
    got = repo.get_by_place_id("ChIJ_X")
    assert got is not None
    assert got.discovered_at == _T1  # preserved from initial stub


def test_upsert_bumps_last_modified_at(repo):
    p = _enriched()
    repo.upsert(p, now=_T2)
    got = repo.get_by_place_id("ChIJ_X")
    assert got.last_modified_at == _T2


# ── queries ──────────────────────────────────────────────────────────


def test_get_pending_returns_only_pending(repo):
    repo.upsert_stub("ChIJ_A", "https://a", now=_T1)
    repo.upsert(_enriched("ChIJ_B"))
    repo.upsert_stub("ChIJ_C", "https://c", now=_T1 + timedelta(seconds=1))
    pending = repo.get_pending()
    assert {p.place_id for p in pending} == {"ChIJ_A", "ChIJ_C"}


def test_get_pending_orders_by_discovered_at(repo):
    repo.upsert_stub("ChIJ_C", "https://c", now=_T1 + timedelta(hours=2))
    repo.upsert_stub("ChIJ_A", "https://a", now=_T1)
    repo.upsert_stub("ChIJ_B", "https://b", now=_T1 + timedelta(hours=1))
    pending = repo.get_pending()
    assert [p.place_id for p in pending] == ["ChIJ_A", "ChIJ_B", "ChIJ_C"]


def test_get_pending_respects_limit(repo):
    for i in range(5):
        repo.upsert_stub(f"ChIJ_{i}", f"https://{i}", now=_T1 + timedelta(seconds=i))
    pending = repo.get_pending(limit=2)
    assert len(pending) == 2


def test_get_stale_returns_old_ok_rows_only(repo):
    now = _T1 + timedelta(days=31)
    fresh = _enriched("ChIJ_FRESH", fetched_at=now - timedelta(days=1))
    stale = _enriched("ChIJ_STALE", fetched_at=_T1)
    erroneous = _enriched(
        "ChIJ_ERR", fetched_at=_T1, fetch_status="error", error_message="boom"
    )
    repo.upsert_many([fresh, stale, erroneous])

    out = repo.get_stale(older_than=timedelta(days=30), now=now)
    # Only the stale 'ok' row qualifies under the default statuses=('ok',).
    assert [p.place_id for p in out] == ["ChIJ_STALE"]


def test_get_stale_can_include_other_statuses_when_requested(repo):
    err = _enriched(
        "ChIJ_E", fetched_at=_T1, fetch_status="error", error_message="boom"
    )
    repo.upsert(err)
    now = _T1 + timedelta(days=10)
    out = repo.get_stale(
        older_than=timedelta(days=1), now=now, statuses=("error",)
    )
    assert [p.place_id for p in out] == ["ChIJ_E"]


def test_count_by_status(repo):
    repo.upsert_stub("ChIJ_A", "https://a")
    repo.upsert_stub("ChIJ_B", "https://b")
    repo.upsert(_enriched("ChIJ_C"))
    counts = repo.count_by_status()
    assert counts == {"pending": 2, "ok": 1}


# ── empty / edge cases ──────────────────────────────────────────────


def test_get_by_place_id_returns_none_when_absent(repo):
    assert repo.get_by_place_id("nope") is None


def test_upsert_many_handles_empty_iterable(repo):
    repo.upsert_many([])
    assert repo.count_by_status() == {}


def test_init_schema_is_idempotent():
    r = PractoProfileRepository(":memory:")
    r.init_schema()
    r.init_schema()
    r.close()
