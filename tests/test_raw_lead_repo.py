from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from zelda.models.raw_lead import RawLead
from zelda.repositories.raw_lead_repo import RawLeadRepository


# ── timestamps used as test data ─────────────────────────────────────────


_T1 = datetime(2026, 4, 29, 10, 0, 0, tzinfo=timezone.utc)
_T2 = _T1 + timedelta(hours=1)
_T3 = _T1 + timedelta(hours=2)


# ── helpers + fixtures ───────────────────────────────────────────────────


def _mk_lead(place_id: str = "ChIJ_X", city: str = "Ludhiana", **overrides: Any) -> RawLead:
    base: dict[str, Any] = dict(
        place_id=place_id,
        city=city,
        name="Test Clinic",
        discovered_at=_T1,
        last_modified_at=_T1,
    )
    base.update(overrides)
    return RawLead(**base)


@pytest.fixture
def repo():
    r = RawLeadRepository(":memory:")
    yield r
    r.close()


# ── schema ──────────────────────────────────────────────────────────────


def test_init_schema_is_idempotent():
    r = RawLeadRepository(":memory:")
    r.init_schema()
    r.init_schema()  # must not raise
    r.close()


def test_repo_creates_parent_dir_if_missing(tmp_path: Path):
    db = tmp_path / "deep" / "subdir" / "x.db"
    r = RawLeadRepository(db)
    try:
        assert db.parent.exists()
    finally:
        r.close()


def test_repo_is_context_manager(tmp_path: Path):
    db = tmp_path / "x.db"
    with RawLeadRepository(db) as r:
        r.upsert_many([_mk_lead("ChIJ_X")])
        assert r.exists("ChIJ_X")


# ── upsert: insert path ─────────────────────────────────────────────────


def test_upsert_inserts_a_new_lead(repo: RawLeadRepository):
    repo.upsert_many([_mk_lead("ChIJ_1")])
    assert repo.exists("ChIJ_1")
    assert repo.count_for_city("Ludhiana") == 1


def test_upsert_inserts_many_in_one_call(repo: RawLeadRepository):
    repo.upsert_many([_mk_lead(f"ChIJ_{i}") for i in range(5)])
    assert repo.count_for_city("Ludhiana") == 5


def test_upsert_handles_empty_list(repo: RawLeadRepository):
    repo.upsert_many([])  # must not raise
    assert repo.count_for_city("Ludhiana") == 0


# ── upsert: update path (the contract that drives delta-sync) ───────────


def test_upsert_updates_mutable_fields_on_conflict(repo: RawLeadRepository):
    repo.upsert_many([_mk_lead("ChIJ_X", rating=4.0, name="Clinic A")])
    repo.upsert_many(
        [_mk_lead("ChIJ_X", rating=4.5, name="Clinic A Renamed", last_modified_at=_T2)]
    )

    lead = repo.get_by_id("ChIJ_X")
    assert lead is not None
    assert lead.rating == 4.5
    assert lead.name == "Clinic A Renamed"


def test_upsert_preserves_discovered_at(repo: RawLeadRepository):
    """`discovered_at` is immutable: re-upsert with a different value
    must not overwrite the original."""
    repo.upsert_many([_mk_lead("ChIJ_X", discovered_at=_T1, last_modified_at=_T1)])
    repo.upsert_many([_mk_lead("ChIJ_X", discovered_at=_T3, last_modified_at=_T3)])

    lead = repo.get_by_id("ChIJ_X")
    assert lead is not None
    assert lead.discovered_at == _T1


def test_upsert_updates_last_modified_at(repo: RawLeadRepository):
    repo.upsert_many([_mk_lead("ChIJ_X", last_modified_at=_T1)])
    repo.upsert_many([_mk_lead("ChIJ_X", last_modified_at=_T2)])

    lead = repo.get_by_id("ChIJ_X")
    assert lead is not None
    assert lead.last_modified_at == _T2


def test_upsert_preserves_last_synced_at(repo: RawLeadRepository):
    """Re-upserting must NOT clobber `last_synced_at` — sync state is owned
    by the sync controller, not the discovery flow."""
    repo.upsert_many([_mk_lead("ChIJ_X", last_modified_at=_T1)])
    repo.mark_synced(["ChIJ_X"], synced_at=_T2)
    repo.upsert_many([_mk_lead("ChIJ_X", last_modified_at=_T3)])

    lead = repo.get_by_id("ChIJ_X")
    assert lead is not None
    assert lead.last_synced_at == _T2


# ── exists / get_by_id ──────────────────────────────────────────────────


def test_exists_false_for_unknown(repo: RawLeadRepository):
    assert repo.exists("ChIJ_NOPE") is False


def test_exists_true_after_upsert(repo: RawLeadRepository):
    repo.upsert_many([_mk_lead("ChIJ_X")])
    assert repo.exists("ChIJ_X") is True


def test_exists_many_returns_only_known_subset(repo: RawLeadRepository):
    repo.upsert_many([_mk_lead("ChIJ_1"), _mk_lead("ChIJ_2")])
    known = repo.exists_many(["ChIJ_1", "ChIJ_MISSING", "ChIJ_2", "ChIJ_OTHER"])
    assert known == {"ChIJ_1", "ChIJ_2"}


def test_exists_many_handles_empty_input(repo: RawLeadRepository):
    assert repo.exists_many([]) == set()


def test_exists_many_returns_empty_when_nothing_known(repo: RawLeadRepository):
    assert repo.exists_many(["ChIJ_NOPE_1", "ChIJ_NOPE_2"]) == set()


def test_get_by_id_returns_lead(repo: RawLeadRepository):
    repo.upsert_many([_mk_lead("ChIJ_X", name="Test")])
    lead = repo.get_by_id("ChIJ_X")
    assert lead is not None
    assert lead.name == "Test"


def test_get_by_id_returns_none_for_unknown(repo: RawLeadRepository):
    assert repo.get_by_id("ChIJ_NOPE") is None


# ── get/count for city ─────────────────────────────────────────────────


def test_count_for_city_filters_by_city(repo: RawLeadRepository):
    repo.upsert_many(
        [
            _mk_lead("ChIJ_L1", city="Ludhiana"),
            _mk_lead("ChIJ_L2", city="Ludhiana"),
            _mk_lead("ChIJ_M1", city="Mumbai"),
        ]
    )
    assert repo.count_for_city("Ludhiana") == 2
    assert repo.count_for_city("Mumbai") == 1
    assert repo.count_for_city("Bengaluru") == 0


def test_get_for_city_returns_only_that_citys_leads(repo: RawLeadRepository):
    repo.upsert_many(
        [
            _mk_lead("ChIJ_L1", city="Ludhiana"),
            _mk_lead("ChIJ_L2", city="Ludhiana"),
            _mk_lead("ChIJ_M1", city="Mumbai"),
        ]
    )
    leads = repo.get_for_city("Ludhiana")
    assert {l.place_id for l in leads} == {"ChIJ_L1", "ChIJ_L2"}


# ── delta sync logic ───────────────────────────────────────────────────


def test_get_unsynced_returns_all_when_none_synced(repo: RawLeadRepository):
    repo.upsert_many([_mk_lead(f"ChIJ_{i}") for i in range(3)])
    unsynced = repo.get_unsynced_for_city("Ludhiana")
    assert len(unsynced) == 3


def test_mark_synced_excludes_marked_rows_from_unsynced(repo: RawLeadRepository):
    repo.upsert_many([_mk_lead(f"ChIJ_{i}", last_modified_at=_T1) for i in range(3)])
    repo.mark_synced(["ChIJ_0", "ChIJ_1"], synced_at=_T2)

    unsynced = repo.get_unsynced_for_city("Ludhiana")
    assert {l.place_id for l in unsynced} == {"ChIJ_2"}


def test_mark_synced_handles_empty_list(repo: RawLeadRepository):
    repo.mark_synced([])  # must not raise


def test_unsynced_after_modification(repo: RawLeadRepository):
    """Delta-detection contract: re-upserting a previously-synced lead
    must put it back into `unsynced` so the sync controller will push
    the update."""
    repo.upsert_many([_mk_lead("ChIJ_X", last_modified_at=_T1, rating=4.0)])
    repo.mark_synced(["ChIJ_X"], synced_at=_T2)

    assert repo.count_unsynced_for_city("Ludhiana") == 0  # currently in sync

    # Re-upsert with a strictly later last_modified_at
    repo.upsert_many([_mk_lead("ChIJ_X", last_modified_at=_T3, rating=4.5)])

    unsynced = repo.get_unsynced_for_city("Ludhiana")
    assert len(unsynced) == 1
    assert unsynced[0].rating == 4.5


def test_count_unsynced_for_city(repo: RawLeadRepository):
    repo.upsert_many(
        [
            _mk_lead("ChIJ_L1", city="Ludhiana", last_modified_at=_T1),
            _mk_lead("ChIJ_L2", city="Ludhiana", last_modified_at=_T1),
            _mk_lead("ChIJ_M1", city="Mumbai", last_modified_at=_T1),
        ]
    )
    repo.mark_synced(["ChIJ_L1"], synced_at=_T2)

    assert repo.count_unsynced_for_city("Ludhiana") == 1
    assert repo.count_unsynced_for_city("Mumbai") == 1
    assert repo.count_unsynced_for_city("Bengaluru") == 0


# ── round-trip fidelity ────────────────────────────────────────────────


def test_extras_and_raw_json_round_trip(repo: RawLeadRepository):
    extras = {"weird": [1, 2, {"nested": True}]}
    raw_json = {"id": "ChIJ_X", "displayName": {"text": "Test"}}

    repo.upsert_many([_mk_lead("ChIJ_X", extras=extras, raw_json=raw_json)])
    lead = repo.get_by_id("ChIJ_X")

    assert lead is not None
    assert lead.extras == extras
    assert lead.raw_json == raw_json


def test_optional_fields_round_trip_as_null(repo: RawLeadRepository):
    """A sparse RawLead (only required fields set) must round-trip with
    all optional fields as None — no '' / 0 / [] sneaking in."""
    repo.upsert_many([_mk_lead("ChIJ_X")])
    lead = repo.get_by_id("ChIJ_X")

    assert lead is not None
    assert lead.phone is None
    assert lead.website is None
    assert lead.types is None
    assert lead.reviews is None
    assert lead.last_synced_at is None


def test_full_field_round_trip(repo: RawLeadRepository):
    """Build a lead with every optional field set, write it, read it back,
    must be equal."""
    lead_in = _mk_lead(
        "ChIJ_FULL",
        formatted_address="123 Main St, Ludhiana",
        short_address="123 Main St",
        address_components=[{"longText": "123", "types": ["street_number"]}],
        lat=30.9,
        lng=75.85,
        phone="098765 43210",
        phone_intl="+91 98765 43210",
        website="https://example.com",
        google_maps_url="https://maps.google.com/?cid=123",
        rating=4.6,
        review_count=87,
        reviews=[{"rating": 5, "text": {"text": "Great"}}],
        business_status="OPERATIONAL",
        primary_type="dentist",
        types=["dentist", "doctor"],
        price_level="PRICE_LEVEL_MODERATE",
        editorial_summary="Sample blurb",
        photos_count=10,
        opening_hours={"openNow": True, "weekdayDescriptions": ["Mon: 9-5"]},
        extras={"foo": "bar"},
        raw_json={"id": "ChIJ_FULL"},
    )
    repo.upsert_many([lead_in])

    lead_out = repo.get_by_id("ChIJ_FULL")
    assert lead_out == lead_in
