"""Tests for `PractoListingRepository` — schema, upsert semantics,
delta-detection, and round-trip correctness.

Mirrors the shape of `test_google_places_lead_repo.py` because the
repos share their lifecycle contract: per-source listing table,
upsert-with-immutable-discovered_at, sync-tracked via
`last_synced_at`.
"""

from datetime import datetime, timedelta, timezone

import pytest

from zelda.models.practo_listing import PractoListing
from zelda.repositories.practo_listing_repo import PractoListingRepository


_T1 = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
_T2 = _T1 + timedelta(hours=1)
_T3 = _T1 + timedelta(days=7)


def _mk(url: str, **overrides) -> PractoListing:
    base = dict(
        profile_url=url,
        city="Ludhiana",
        name="Test Clinic",
        address="123 Test St",
        lat=30.9,
        lng=75.85,
        raw_json={},
        discovered_at=_T1,
        last_modified_at=_T1,
    )
    base.update(overrides)
    return PractoListing(**base)


@pytest.fixture
def repo():
    r = PractoListingRepository(":memory:")
    yield r
    r.close()


# ── schema + lifecycle ──────────────────────────────────────────────


def test_init_schema_is_idempotent():
    r = PractoListingRepository(":memory:")
    r.init_schema()  # second call should not error
    r.close()


def test_get_by_url_returns_none_when_absent(repo):
    assert repo.get_by_url("https://example.com/missing") is None


def test_count_for_city_empty(repo):
    assert repo.count_for_city("Ludhiana") == 0


# ── upsert + read round-trip ────────────────────────────────────────


def test_upsert_and_read_round_trip(repo):
    repo.upsert_many([_mk("u1"), _mk("u2", name="Other Clinic")])
    assert repo.count_for_city("Ludhiana") == 2
    got = repo.get_by_url("u1")
    assert got is not None
    assert got.name == "Test Clinic"
    assert got.lat == 30.9


def test_upsert_preserves_discovered_at_on_update(repo):
    repo.upsert_many([_mk("u1", discovered_at=_T1, last_modified_at=_T1)])
    repo.upsert_many([
        _mk("u1", name="Updated", discovered_at=_T2, last_modified_at=_T2)
    ])
    got = repo.get_by_url("u1")
    assert got is not None
    assert got.name == "Updated"
    assert got.discovered_at == _T1  # immutable
    assert got.last_modified_at == _T2  # bumped


def test_upsert_does_not_clobber_last_synced_at(repo):
    repo.upsert_many([_mk("u1", last_synced_at=_T2)])
    # Subsequent upsert without last_synced_at must NOT clear it
    repo.upsert_many([_mk("u1", name="Renamed", last_synced_at=None)])
    got = repo.get_by_url("u1")
    assert got is not None
    assert got.name == "Renamed"
    assert got.last_synced_at == _T2  # preserved


def test_upsert_many_empty_is_noop(repo):
    repo.upsert_many([])
    assert repo.count_for_city("Ludhiana") == 0


# ── existence checks ────────────────────────────────────────────────


def test_exists_and_exists_many(repo):
    repo.upsert_many([_mk("u1"), _mk("u2")])
    assert repo.exists("u1") is True
    assert repo.exists("u_missing") is False
    assert repo.exists_many(["u1", "u2", "u_missing"]) == {"u1", "u2"}
    assert repo.exists_many([]) == set()


# ── city scoping ────────────────────────────────────────────────────


def test_get_for_city_filters_correctly(repo):
    repo.upsert_many([
        _mk("u1", city="Ludhiana"),
        _mk("u2", city="Ludhiana"),
        _mk("u3", city="Mumbai"),
    ])
    ldh = repo.get_for_city("Ludhiana")
    assert {l.profile_url for l in ldh} == {"u1", "u2"}
    mum = repo.get_for_city("Mumbai")
    assert {l.profile_url for l in mum} == {"u3"}


def test_get_for_city_orders_by_discovered_at(repo):
    repo.upsert_many([
        _mk("u_late", discovered_at=_T3),
        _mk("u_early", discovered_at=_T1),
        _mk("u_mid", discovered_at=_T2),
    ])
    rows = repo.get_for_city("Ludhiana")
    assert [r.profile_url for r in rows] == ["u_early", "u_mid", "u_late"]


# ── delta-detection / sync ──────────────────────────────────────────


def test_unsynced_includes_never_synced_rows(repo):
    repo.upsert_many([_mk("u1"), _mk("u2")])
    out = repo.get_unsynced_for_city("Ludhiana")
    assert {r.profile_url for r in out} == {"u1", "u2"}
    assert repo.count_unsynced_for_city("Ludhiana") == 2


def test_unsynced_excludes_recently_synced(repo):
    repo.upsert_many([_mk("u1", last_modified_at=_T1)])
    repo.mark_synced(["u1"], synced_at=_T2)
    assert repo.count_unsynced_for_city("Ludhiana") == 0


def test_unsynced_includes_modified_since_last_sync(repo):
    repo.upsert_many([_mk("u1", last_modified_at=_T1)])
    repo.mark_synced(["u1"], synced_at=_T2)
    # Now bump last_modified_at past last_synced_at
    repo.upsert_many([_mk("u1", name="Renamed", last_modified_at=_T3)])
    out = repo.get_unsynced_for_city("Ludhiana")
    assert {r.profile_url for r in out} == {"u1"}


def test_mark_synced_empty_is_noop(repo):
    repo.mark_synced([])  # no error


# ── round-trip nullability ──────────────────────────────────────────


def test_round_trip_with_minimal_fields(repo):
    """Address / lat / lng are optional — must round-trip as None."""
    repo.upsert_many([_mk("u1", address=None, lat=None, lng=None)])
    got = repo.get_by_url("u1")
    assert got is not None
    assert got.address is None
    assert got.lat is None
    assert got.lng is None


def test_raw_json_round_trip(repo):
    repo.upsert_many([_mk("u1", raw_json={"a": 1, "b": ["x", "y"]})])
    got = repo.get_by_url("u1")
    assert got is not None
    assert got.raw_json == {"a": 1, "b": ["x", "y"]}
