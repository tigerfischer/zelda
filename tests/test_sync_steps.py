"""Tests for the three concrete `SyncStep` implementations.

Each step is exercised against:
- `FakeDriveGateway` (in-memory Drive)
- a real `:memory:` SQLite repo (so delta-detection SQL is tested)
- a tmp_path for JSONL artifacts (Google Places only)

All tests are pure unit tests — no network, no filesystem beyond tmp_path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import FakeDriveGateway
from zelda.controllers.sync_steps import (
    DISCOVERY_FOLDER_NAME,
    GOOGLE_PLACES_HEADER,
    LYBRATE_HEADER,
    PRACTO_HEADER,
    GooglePlacesSyncStep,
    LybrateSyncStep,
    PractoSyncStep,
)
from zelda.models.google_places_lead import GooglePlacesLead
from zelda.models.lybrate_listing import LybrateListing
from zelda.models.practo_listing import PractoListing
from zelda.repositories.google_places_lead_repo import GooglePlacesLeadRepository
from zelda.repositories.lybrate_listing_repo import LybrateListingRepository
from zelda.repositories.practo_listing_repo import PractoListingRepository
from zelda.util import slugify


_T1 = datetime(2026, 4, 29, 10, 0, 0, tzinfo=timezone.utc)
_T2 = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
_RUN_ID = "sync-test-001"


# ── model factories ─────────────────────────────────────────────────


def _gp_lead(
    place_id: str = "p1",
    city: str = "Ludhiana",
    *,
    last_modified_at: datetime = _T1,
    last_synced_at: datetime | None = None,
    **kw: Any,
) -> GooglePlacesLead:
    return GooglePlacesLead(
        place_id=place_id,
        city=city,
        name=f"Clinic {place_id}",
        discovered_at=last_modified_at,
        last_modified_at=last_modified_at,
        last_synced_at=last_synced_at,
        **kw,
    )


def _practo(
    profile_url: str = "https://practo.com/ludhiana/clinic/c1",
    city: str = "Ludhiana",
    *,
    last_modified_at: datetime = _T1,
    last_synced_at: datetime | None = None,
) -> PractoListing:
    return PractoListing(
        profile_url=profile_url,
        city=city,
        name="Clinic " + profile_url.split("/")[-1],
        discovered_at=last_modified_at,
        last_modified_at=last_modified_at,
        last_synced_at=last_synced_at,
    )


def _lybrate(
    profile_url: str = "https://lybrate.com/ludhiana/dentist/dr-one",
    city: str = "Ludhiana",
    *,
    last_modified_at: datetime = _T1,
    last_synced_at: datetime | None = None,
) -> LybrateListing:
    return LybrateListing(
        profile_url=profile_url,
        city=city,
        doctor_name="Dr " + profile_url.split("/")[-1],
        discovered_at=last_modified_at,
        last_modified_at=last_modified_at,
        last_synced_at=last_synced_at,
    )


# ── GooglePlacesSyncStep ────────────────────────────────────────────


@pytest.fixture
def gp_repo():
    r = GooglePlacesLeadRepository(":memory:")
    yield r
    r.close()


def _gp_step(drive, repo, artifacts_dir) -> GooglePlacesSyncStep:
    return GooglePlacesSyncStep(
        drive=drive, repo=repo, artifacts_dir=artifacts_dir, clock=lambda: _T2,
    )


def test_gp_no_unsynced_skips_sheet_upsert(tmp_path):
    drive = FakeDriveGateway()
    repo = GooglePlacesLeadRepository(":memory:")
    step = _gp_step(drive, repo, tmp_path / "artifacts")

    result = step.run_for_city("Ludhiana", run_id=_RUN_ID)

    assert drive.upsert_calls == []
    assert result.pulled == 0
    assert result.inserted == 0


def test_gp_syncs_rows_to_discovery_sheet(tmp_path):
    drive = FakeDriveGateway()
    repo = GooglePlacesLeadRepository(":memory:")
    repo.upsert_many([_gp_lead("p1"), _gp_lead("p2")])
    step = _gp_step(drive, repo, tmp_path / "artifacts")

    result = step.run_for_city("Ludhiana", run_id=_RUN_ID)

    assert len(drive.upsert_calls) == 1
    call = drive.upsert_calls[0]
    assert call["header"] == GOOGLE_PLACES_HEADER
    assert call["key_column"] == "place_id"
    assert {r["place_id"] for r in call["rows"]} == {"p1", "p2"}
    assert result.pulled == 2


def test_gp_sheet_lives_under_city_discovery_folder(tmp_path):
    """Sheet must be at {root}/{City}/discovery/google_places."""
    drive = FakeDriveGateway()
    repo = GooglePlacesLeadRepository(":memory:")
    repo.upsert_many([_gp_lead()])
    step = _gp_step(drive, repo, tmp_path / "artifacts")

    result = step.run_for_city("Ludhiana", run_id=_RUN_ID)

    sheet_id = result.extras["sheet_id"]
    # Verify sheet is in the discovery folder, which is under the city folder
    city_folder = drive.find_or_create_subfolder("Ludhiana")
    disc_folder = drive.find_or_create_subfolder(DISCOVERY_FOLDER_NAME, parent_folder_id=city_folder)
    sheets_in_disc = {f["id"] for f in drive.list_files_in_folder(disc_folder)}
    assert sheet_id in sheets_in_disc


def test_gp_marks_synced_after_drive_write(tmp_path):
    drive = FakeDriveGateway()
    repo = GooglePlacesLeadRepository(":memory:")
    repo.upsert_many([_gp_lead("p1"), _gp_lead("p2")])
    step = _gp_step(drive, repo, tmp_path / "artifacts")

    step.run_for_city("Ludhiana", run_id=_RUN_ID)

    assert repo.count_unsynced_for_city("Ludhiana") == 0
    lead = repo.get_by_id("p1")
    assert lead is not None
    assert lead.last_synced_at == _T2


def test_gp_sheet_failure_leaves_rows_pending(tmp_path):
    drive = FakeDriveGateway()
    repo = GooglePlacesLeadRepository(":memory:")
    repo.upsert_many([_gp_lead("p1")])

    def boom(*a, **kw):
        raise RuntimeError("Drive 503")
    drive.upsert_sheet_rows_by_key = boom  # type: ignore

    step = _gp_step(drive, repo, tmp_path / "artifacts")
    result = step.run_for_city("Ludhiana", run_id=_RUN_ID)

    assert any("sheet sync failed" in e for e in result.errors)
    assert repo.count_unsynced_for_city("Ludhiana") == 1


def test_gp_artifact_upload(tmp_path):
    drive = FakeDriveGateway()
    repo = GooglePlacesLeadRepository(":memory:")
    city_slug = slugify("Ludhiana")
    art_dir = tmp_path / "artifacts"
    city_art = art_dir / city_slug
    city_art.mkdir(parents=True)
    (city_art / "run-001.jsonl").write_text('{"id":"p1"}\n')
    (city_art / "run-002.jsonl").write_text('{"id":"p2"}\n')

    step = _gp_step(drive, repo, art_dir)
    result = step.run_for_city("Ludhiana", run_id=_RUN_ID)

    assert result.extras["artifacts_uploaded"] == 2
    assert result.extras["artifacts_skipped"] == 0
    assert len(drive.upload_calls) == 2
    uploaded = {c["drive_name"] for c in drive.upload_calls}
    assert uploaded == {"run-001.jsonl", "run-002.jsonl"}


def test_gp_artifact_skips_existing_on_drive(tmp_path):
    drive = FakeDriveGateway()
    repo = GooglePlacesLeadRepository(":memory:")
    city_slug = slugify("Ludhiana")
    art_dir = tmp_path / "artifacts"
    city_art = art_dir / city_slug
    city_art.mkdir(parents=True)
    (city_art / "run-001.jsonl").write_text('{"id":"p1"}\n')
    (city_art / "run-002.jsonl").write_text('{"id":"p2"}\n')

    # Pre-seed drive with run-001 already there
    city_folder = drive.find_or_create_subfolder("Ludhiana")
    disc_folder = drive.find_or_create_subfolder(DISCOVERY_FOLDER_NAME, parent_folder_id=city_folder)
    art_folder = drive.find_or_create_subfolder("raw-artifacts", parent_folder_id=disc_folder)
    drive.add_file_to_folder(art_folder, "run-001.jsonl", b"x")

    step = _gp_step(drive, repo, art_dir)
    result = step.run_for_city("Ludhiana", run_id=_RUN_ID)

    assert result.extras["artifacts_uploaded"] == 1
    assert result.extras["artifacts_skipped"] == 1
    assert {c["drive_name"] for c in drive.upload_calls} == {"run-002.jsonl"}


def test_gp_artifact_sync_runs_even_when_no_unsynced_rows(tmp_path):
    drive = FakeDriveGateway()
    repo = GooglePlacesLeadRepository(":memory:")
    art_dir = tmp_path / "artifacts"
    city_art = art_dir / slugify("Ludhiana")
    city_art.mkdir(parents=True)
    (city_art / "run-001.jsonl").write_text('{"id":"p1"}\n')

    step = _gp_step(drive, repo, art_dir)
    result = step.run_for_city("Ludhiana", run_id=_RUN_ID)

    assert result.pulled == 0
    assert drive.upsert_calls == []  # no sheet sync
    assert result.extras["artifacts_uploaded"] == 1  # artifact still uploaded


def test_gp_second_run_is_noop_for_sheet(tmp_path):
    drive = FakeDriveGateway()
    repo = GooglePlacesLeadRepository(":memory:")
    repo.upsert_many([_gp_lead("p1")])
    step = _gp_step(drive, repo, tmp_path / "artifacts")

    step.run_for_city("Ludhiana", run_id=_RUN_ID)
    drive.upsert_calls.clear()

    result2 = step.run_for_city("Ludhiana", run_id=_RUN_ID + "-2")

    assert result2.pulled == 0
    assert drive.upsert_calls == []


def test_gp_only_syncs_rows_for_requested_city(tmp_path):
    drive = FakeDriveGateway()
    repo = GooglePlacesLeadRepository(":memory:")
    repo.upsert_many([_gp_lead("p1", city="Ludhiana"), _gp_lead("p2", city="Mumbai")])
    step = _gp_step(drive, repo, tmp_path / "artifacts")

    step.run_for_city("Ludhiana", run_id=_RUN_ID)

    sent = {r["place_id"] for r in drive.upsert_calls[0]["rows"]}
    assert sent == {"p1"}
    assert repo.get_by_id("p2").last_synced_at is None  # type: ignore[union-attr]


# ── PractoSyncStep ──────────────────────────────────────────────────


def _practo_step(drive, repo) -> PractoSyncStep:
    return PractoSyncStep(drive=drive, repo=repo, clock=lambda: _T2)


def test_practo_no_unsynced_is_noop():
    drive = FakeDriveGateway()
    repo = PractoListingRepository(":memory:")
    step = _practo_step(drive, repo)

    result = step.run_for_city("Ludhiana", run_id=_RUN_ID)

    assert drive.upsert_calls == []
    assert result.pulled == 0


def test_practo_syncs_rows_to_discovery_sheet():
    drive = FakeDriveGateway()
    repo = PractoListingRepository(":memory:")
    repo.upsert_many([_practo("https://practo.com/ludhiana/clinic/c1"),
                      _practo("https://practo.com/ludhiana/clinic/c2")])
    step = _practo_step(drive, repo)

    result = step.run_for_city("Ludhiana", run_id=_RUN_ID)

    assert len(drive.upsert_calls) == 1
    call = drive.upsert_calls[0]
    assert call["header"] == PRACTO_HEADER
    assert call["key_column"] == "profile_url"
    assert len(call["rows"]) == 2
    assert result.pulled == 2


def test_practo_sheet_lives_under_city_discovery_folder():
    drive = FakeDriveGateway()
    repo = PractoListingRepository(":memory:")
    repo.upsert_many([_practo()])
    step = _practo_step(drive, repo)

    result = step.run_for_city("Ludhiana", run_id=_RUN_ID)

    sheet_id = result.extras["sheet_id"]
    city_folder = drive.find_or_create_subfolder("Ludhiana")
    disc_folder = drive.find_or_create_subfolder(DISCOVERY_FOLDER_NAME, parent_folder_id=city_folder)
    sheets_in_disc = {f["id"] for f in drive.list_files_in_folder(disc_folder)}
    assert sheet_id in sheets_in_disc


def test_practo_marks_synced_after_drive_write():
    drive = FakeDriveGateway()
    repo = PractoListingRepository(":memory:")
    repo.upsert_many([_practo("https://practo.com/ludhiana/clinic/c1")])
    step = _practo_step(drive, repo)

    step.run_for_city("Ludhiana", run_id=_RUN_ID)

    assert repo.count_unsynced_for_city("Ludhiana") == 0
    listing = repo.get_by_url("https://practo.com/ludhiana/clinic/c1")
    assert listing is not None
    assert listing.last_synced_at == _T2


def test_practo_sheet_failure_leaves_rows_pending():
    drive = FakeDriveGateway()
    repo = PractoListingRepository(":memory:")
    repo.upsert_many([_practo()])

    def boom(*a, **kw):
        raise RuntimeError("Drive 503")
    drive.upsert_sheet_rows_by_key = boom  # type: ignore

    step = _practo_step(drive, repo)
    result = step.run_for_city("Ludhiana", run_id=_RUN_ID)

    assert any("sheet sync failed" in e for e in result.errors)
    assert repo.count_unsynced_for_city("Ludhiana") == 1


def test_practo_only_syncs_requested_city():
    drive = FakeDriveGateway()
    repo = PractoListingRepository(":memory:")
    repo.upsert_many([
        _practo("https://practo.com/ludhiana/clinic/c1", city="Ludhiana"),
        _practo("https://practo.com/mumbai/clinic/c2", city="Mumbai"),
    ])
    step = _practo_step(drive, repo)

    step.run_for_city("Ludhiana", run_id=_RUN_ID)

    sent = {r["profile_url"] for r in drive.upsert_calls[0]["rows"]}
    assert "https://practo.com/ludhiana/clinic/c1" in sent
    assert "https://practo.com/mumbai/clinic/c2" not in sent


# ── LybrateSyncStep ─────────────────────────────────────────────────


def _lybrate_step(drive, repo) -> LybrateSyncStep:
    return LybrateSyncStep(drive=drive, repo=repo, clock=lambda: _T2)


def test_lybrate_no_unsynced_is_noop():
    drive = FakeDriveGateway()
    repo = LybrateListingRepository(":memory:")
    step = _lybrate_step(drive, repo)

    result = step.run_for_city("Ludhiana", run_id=_RUN_ID)

    assert drive.upsert_calls == []
    assert result.pulled == 0


def test_lybrate_syncs_rows_to_discovery_sheet():
    drive = FakeDriveGateway()
    repo = LybrateListingRepository(":memory:")
    repo.upsert_many([
        _lybrate("https://lybrate.com/ludhiana/dentist/dr-one"),
        _lybrate("https://lybrate.com/ludhiana/dentist/dr-two"),
    ])
    step = _lybrate_step(drive, repo)

    result = step.run_for_city("Ludhiana", run_id=_RUN_ID)

    assert len(drive.upsert_calls) == 1
    call = drive.upsert_calls[0]
    assert call["header"] == LYBRATE_HEADER
    assert call["key_column"] == "profile_url"
    assert len(call["rows"]) == 2
    assert result.pulled == 2


def test_lybrate_sheet_lives_under_city_discovery_folder():
    drive = FakeDriveGateway()
    repo = LybrateListingRepository(":memory:")
    repo.upsert_many([_lybrate()])
    step = _lybrate_step(drive, repo)

    result = step.run_for_city("Ludhiana", run_id=_RUN_ID)

    sheet_id = result.extras["sheet_id"]
    city_folder = drive.find_or_create_subfolder("Ludhiana")
    disc_folder = drive.find_or_create_subfolder(DISCOVERY_FOLDER_NAME, parent_folder_id=city_folder)
    sheets_in_disc = {f["id"] for f in drive.list_files_in_folder(disc_folder)}
    assert sheet_id in sheets_in_disc


def test_lybrate_marks_synced_after_drive_write():
    drive = FakeDriveGateway()
    repo = LybrateListingRepository(":memory:")
    url = "https://lybrate.com/ludhiana/dentist/dr-one"
    repo.upsert_many([_lybrate(url)])
    step = _lybrate_step(drive, repo)

    step.run_for_city("Ludhiana", run_id=_RUN_ID)

    assert repo.count_unsynced_for_city("Ludhiana") == 0
    listing = repo.get_by_url(url)
    assert listing is not None
    assert listing.last_synced_at == _T2


def test_lybrate_sheet_failure_leaves_rows_pending():
    drive = FakeDriveGateway()
    repo = LybrateListingRepository(":memory:")
    repo.upsert_many([_lybrate()])

    def boom(*a, **kw):
        raise RuntimeError("Drive 503")
    drive.upsert_sheet_rows_by_key = boom  # type: ignore

    step = _lybrate_step(drive, repo)
    result = step.run_for_city("Ludhiana", run_id=_RUN_ID)

    assert any("sheet sync failed" in e for e in result.errors)
    assert repo.count_unsynced_for_city("Ludhiana") == 1


# ── header sanity ───────────────────────────────────────────────────


def test_google_places_header_no_duplicates():
    assert len(GOOGLE_PLACES_HEADER) == len(set(GOOGLE_PLACES_HEADER))


def test_practo_header_no_duplicates():
    assert len(PRACTO_HEADER) == len(set(PRACTO_HEADER))


def test_lybrate_header_no_duplicates():
    assert len(LYBRATE_HEADER) == len(set(LYBRATE_HEADER))
