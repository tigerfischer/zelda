"""Tests for `LybrateListingRepository`. Same shape as the Practo
listing repo tests — different fields, identical lifecycle contract."""

from datetime import datetime, timedelta, timezone

import pytest

from zelda.models.lybrate_listing import LybrateListing
from zelda.repositories.lybrate_listing_repo import LybrateListingRepository


_T1 = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
_T2 = _T1 + timedelta(hours=1)
_T3 = _T1 + timedelta(days=7)


def _mk(url: str, **overrides) -> LybrateListing:
    base = dict(
        profile_url=url,
        city="Ludhiana",
        doctor_name="Dr. X",
        clinic_name="X Clinic",
        address="123 Test St",
        locality="Model Town",
        postal_code="141001",
        lat=30.9,
        lng=75.85,
        phone="+91 9000000000",
        specialty="Dentist",
        raw_json={},
        discovered_at=_T1,
        last_modified_at=_T1,
    )
    base.update(overrides)
    return LybrateListing(**base)


@pytest.fixture
def repo():
    r = LybrateListingRepository(":memory:")
    yield r
    r.close()


# ── schema + lifecycle ──────────────────────────────────────────────


def test_init_schema_is_idempotent():
    r = LybrateListingRepository(":memory:")
    r.init_schema()
    r.close()


def test_get_by_url_returns_none_when_absent(repo):
    assert repo.get_by_url("https://example.com/missing") is None


# ── upsert + read round-trip ────────────────────────────────────────


def test_upsert_and_read_round_trip(repo):
    repo.upsert_many([_mk("u1"), _mk("u2", doctor_name="Dr. Y")])
    assert repo.count_for_city("Ludhiana") == 2
    got = repo.get_by_url("u1")
    assert got is not None
    assert got.doctor_name == "Dr. X"
    assert got.specialty == "Dentist"


def test_upsert_preserves_discovered_at_on_update(repo):
    repo.upsert_many([_mk("u1", discovered_at=_T1, last_modified_at=_T1)])
    repo.upsert_many([
        _mk("u1", doctor_name="Updated", discovered_at=_T2, last_modified_at=_T2)
    ])
    got = repo.get_by_url("u1")
    assert got is not None
    assert got.doctor_name == "Updated"
    assert got.discovered_at == _T1
    assert got.last_modified_at == _T2


def test_upsert_does_not_clobber_last_synced_at(repo):
    repo.upsert_many([_mk("u1", last_synced_at=_T2)])
    repo.upsert_many([_mk("u1", doctor_name="Renamed", last_synced_at=None)])
    got = repo.get_by_url("u1")
    assert got is not None
    assert got.last_synced_at == _T2


def test_upsert_many_empty_is_noop(repo):
    repo.upsert_many([])
    assert repo.count_for_city("Ludhiana") == 0


# ── existence checks ────────────────────────────────────────────────


def test_exists_and_exists_many(repo):
    repo.upsert_many([_mk("u1"), _mk("u2")])
    assert repo.exists("u1") is True
    assert repo.exists("missing") is False
    assert repo.exists_many(["u1", "missing", "u2"]) == {"u1", "u2"}
    assert repo.exists_many([]) == set()


# ── city scoping ────────────────────────────────────────────────────


def test_get_for_city_filters_correctly(repo):
    repo.upsert_many([
        _mk("u1", city="Ludhiana"),
        _mk("u2", city="Mumbai"),
    ])
    assert [l.profile_url for l in repo.get_for_city("Ludhiana")] == ["u1"]


def test_get_for_city_orders_by_discovered_at(repo):
    repo.upsert_many([
        _mk("u_late", discovered_at=_T3),
        _mk("u_early", discovered_at=_T1),
    ])
    rows = repo.get_for_city("Ludhiana")
    assert [r.profile_url for r in rows] == ["u_early", "u_late"]


# ── delta-detection / sync ──────────────────────────────────────────


def test_unsynced_includes_never_synced_rows(repo):
    repo.upsert_many([_mk("u1"), _mk("u2")])
    assert repo.count_unsynced_for_city("Ludhiana") == 2


def test_unsynced_excludes_recently_synced(repo):
    repo.upsert_many([_mk("u1")])
    repo.mark_synced(["u1"], synced_at=_T2)
    assert repo.count_unsynced_for_city("Ludhiana") == 0


def test_unsynced_includes_modified_since_last_sync(repo):
    repo.upsert_many([_mk("u1", last_modified_at=_T1)])
    repo.mark_synced(["u1"], synced_at=_T2)
    repo.upsert_many([_mk("u1", doctor_name="Renamed", last_modified_at=_T3)])
    out = repo.get_unsynced_for_city("Ludhiana")
    assert {r.profile_url for r in out} == {"u1"}


def test_mark_synced_empty_is_noop(repo):
    repo.mark_synced([])


# ── round-trip nullability ──────────────────────────────────────────


def test_round_trip_with_minimal_fields(repo):
    repo.upsert_many([_mk(
        "u1",
        clinic_name=None, address=None, locality=None, postal_code=None,
        lat=None, lng=None, phone=None, specialty=None,
    )])
    got = repo.get_by_url("u1")
    assert got is not None
    assert got.clinic_name is None
    assert got.address is None
    assert got.lat is None
    assert got.lng is None
    assert got.phone is None
    assert got.specialty is None


def test_raw_json_round_trip(repo):
    repo.upsert_many([_mk("u1", raw_json={"a": 1, "list": [1, 2]})])
    got = repo.get_by_url("u1")
    assert got is not None
    assert got.raw_json == {"a": 1, "list": [1, 2]}
