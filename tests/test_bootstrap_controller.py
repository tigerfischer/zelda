import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.fakes import FakeDriveGateway
from zelda.controllers.bootstrap import (
    BootstrapController,
    _parse_run_id_timestamp,
)
from zelda.controllers.sync_steps import ARTIFACTS_FOLDER_NAME as ARTIFACTS_DRIVE_FOLDER
from zelda.repositories.google_places_lead_repo import GooglePlacesLeadRepository


# ── helpers ──────────────────────────────────────────────────────────────


def _mk_details(
    place_id: str,
    name: str = "Test Clinic",
    rating: float = 4.5,
    review_count: int = 100,
) -> dict:
    return {
        "id": place_id,
        "displayName": {"text": name, "languageCode": "en"},
        "formattedAddress": "123 Test St, Ludhiana",
        "rating": rating,
        "userRatingCount": review_count,
        "businessStatus": "OPERATIONAL",
        "primaryType": "dentist",
    }


def _seed_drive_with_artifact(
    drive: FakeDriveGateway,
    city_slug: str,
    filename: str,
    place_payloads: list[dict],
) -> str:
    """Create the raw-artifacts/{city_slug}/ folder hierarchy in the
    fake and place a JSONL file with one line per Place Details dict."""
    artifacts_root = drive.find_or_create_subfolder(ARTIFACTS_DRIVE_FOLDER)
    city_folder = drive.find_or_create_subfolder(
        city_slug, parent_folder_id=artifacts_root
    )
    contents = "\n".join(json.dumps(p) for p in place_payloads) + "\n"
    drive.add_file_to_folder(
        city_folder, filename, contents, mime_type="application/x-ndjson"
    )
    return city_folder


@pytest.fixture
def drive() -> FakeDriveGateway:
    return FakeDriveGateway()


@pytest.fixture
def repo():
    r = GooglePlacesLeadRepository(":memory:")
    yield r
    r.close()


@pytest.fixture
def controller(drive: FakeDriveGateway, repo: GooglePlacesLeadRepository, tmp_path: Path) -> BootstrapController:
    return BootstrapController(drive=drive, repo=repo, artifacts_dir=tmp_path / "artifacts")


# ── empty / no-op cases ─────────────────────────────────────────────────


def test_bootstrap_with_no_drive_artifacts_is_noop(controller, drive, repo):
    result = controller.bootstrap_city("Ludhiana")

    assert result.n_drive_artifacts == 0
    assert result.n_files_downloaded == 0
    assert result.n_leads_upserted == 0
    assert repo.count_for_city("Ludhiana") == 0


def test_bootstrap_rejects_blank_city(controller):
    with pytest.raises(ValueError, match="city"):
        controller.bootstrap_city("")
    with pytest.raises(ValueError, match="city"):
        controller.bootstrap_city("   ")


# ── happy path ─────────────────────────────────────────────────────────


def test_bootstrap_pulls_jsonl_from_drive_and_upserts_leads(
    drive, repo, tmp_path
):
    _seed_drive_with_artifact(
        drive,
        "ludhiana",
        "20260429-100000-0001.jsonl",
        [_mk_details("p1", "Sai Dental"), _mk_details("p2", "Saggar")],
    )
    ctrl = BootstrapController(drive=drive, repo=repo, artifacts_dir=tmp_path / "artifacts")

    result = ctrl.bootstrap_city("Ludhiana")

    assert result.n_drive_artifacts == 1
    assert result.n_files_downloaded == 1
    assert result.n_files_skipped_local == 0
    assert result.n_files_processed == 1
    assert result.n_lines_total == 2
    assert result.n_leads_upserted == 2

    # Repo state
    assert repo.count_for_city("Ludhiana") == 2
    p1 = repo.get_by_id("p1")
    assert p1 is not None
    assert p1.name == "Sai Dental"


def test_bootstrap_writes_files_locally(drive, repo, tmp_path):
    _seed_drive_with_artifact(
        drive,
        "ludhiana",
        "20260429-100000-0001.jsonl",
        [_mk_details("p1")],
    )
    artifacts_dir = tmp_path / "artifacts"
    ctrl = BootstrapController(drive=drive, repo=repo, artifacts_dir=artifacts_dir)

    ctrl.bootstrap_city("Ludhiana")

    expected = artifacts_dir / "ludhiana" / "20260429-100000-0001.jsonl"
    assert expected.exists()
    assert "p1" in expected.read_text()


def test_bootstrap_uses_run_id_timestamp_for_discovered_at(drive, repo, tmp_path):
    """`discovered_at` should reflect the original run, parsed from the
    filename — not the current bootstrap time."""
    _seed_drive_with_artifact(
        drive,
        "ludhiana",
        "20260429-125652-ad4e.jsonl",
        [_mk_details("p1")],
    )
    ctrl = BootstrapController(drive=drive, repo=repo, artifacts_dir=tmp_path / "artifacts")

    ctrl.bootstrap_city("Ludhiana")

    p1 = repo.get_by_id("p1")
    assert p1 is not None
    expected = datetime(2026, 4, 29, 12, 56, 52, tzinfo=timezone.utc)
    assert p1.discovered_at == expected


def test_bootstrap_falls_back_to_now_when_filename_unparseable(
    drive, repo, tmp_path
):
    _seed_drive_with_artifact(
        drive,
        "ludhiana",
        "weird-name.jsonl",
        [_mk_details("p1")],
    )
    ctrl = BootstrapController(drive=drive, repo=repo, artifacts_dir=tmp_path / "artifacts")

    before = datetime.now(timezone.utc)
    ctrl.bootstrap_city("Ludhiana")

    p1 = repo.get_by_id("p1")
    assert p1 is not None
    # discovered_at should be set to roughly NOW (the bootstrap time)
    assert p1.discovered_at >= before


def test_bootstrap_processes_files_chronologically(drive, repo, tmp_path):
    """If the same place appears in two JSONLs (re-fetch over time),
    `discovered_at` should equal the OLDEST run's timestamp and
    `last_modified_at` should equal the NEWEST."""
    artifacts_root = drive.find_or_create_subfolder(ARTIFACTS_DRIVE_FOLDER)
    city_folder = drive.find_or_create_subfolder(
        "ludhiana", parent_folder_id=artifacts_root
    )

    # Two JSONLs containing the same place_id, different run timestamps.
    drive.add_file_to_folder(
        city_folder,
        "20260101-100000-0001.jsonl",
        json.dumps(_mk_details("p1", name="Old name", rating=4.0)) + "\n",
        mime_type="application/x-ndjson",
    )
    drive.add_file_to_folder(
        city_folder,
        "20260601-100000-0002.jsonl",
        json.dumps(_mk_details("p1", name="New name", rating=4.7)) + "\n",
        mime_type="application/x-ndjson",
    )

    ctrl = BootstrapController(drive=drive, repo=repo, artifacts_dir=tmp_path / "artifacts")
    ctrl.bootstrap_city("Ludhiana")

    p1 = repo.get_by_id("p1")
    assert p1 is not None
    # discovered_at should be from the oldest file
    assert p1.discovered_at == datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    # last_modified_at should be from the newest file
    assert p1.last_modified_at == datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    # Mutable fields reflect the newest snapshot
    assert p1.name == "New name"
    assert p1.rating == 4.7


def test_bootstrap_skips_files_already_local(drive, repo, tmp_path):
    """Re-running bootstrap on a populated machine doesn't re-download."""
    _seed_drive_with_artifact(
        drive,
        "ludhiana",
        "20260429-100000-0001.jsonl",
        [_mk_details("p1")],
    )
    artifacts_dir = tmp_path / "artifacts"
    ctrl = BootstrapController(drive=drive, repo=repo, artifacts_dir=artifacts_dir)

    ctrl.bootstrap_city("Ludhiana")
    assert len(drive.download_calls) == 1

    # Second run: file is already local
    ctrl.bootstrap_city("Ludhiana")
    assert len(drive.download_calls) == 1  # no additional downloads


def test_bootstrap_marks_leads_as_synced(drive, repo, tmp_path):
    """Bootstrapped leads should be considered in-sync with Drive
    (last_synced_at >= last_modified_at) — a follow-up sync is a no-op."""
    _seed_drive_with_artifact(
        drive,
        "ludhiana",
        "20260101-100000-0001.jsonl",
        [_mk_details("p1")],
    )
    ctrl = BootstrapController(drive=drive, repo=repo, artifacts_dir=tmp_path / "artifacts")

    ctrl.bootstrap_city("Ludhiana")

    assert repo.count_unsynced_for_city("Ludhiana") == 0
    p1 = repo.get_by_id("p1")
    assert p1 is not None
    assert p1.last_synced_at is not None
    # Sanity: last_synced_at >= last_modified_at
    assert p1.last_synced_at >= p1.last_modified_at


# ── error tolerance ────────────────────────────────────────────────────


def test_bootstrap_skips_malformed_jsonl_lines(drive, repo, tmp_path):
    """A bad line should be logged but shouldn't abort the run."""
    artifacts_root = drive.find_or_create_subfolder(ARTIFACTS_DRIVE_FOLDER)
    city_folder = drive.find_or_create_subfolder(
        "ludhiana", parent_folder_id=artifacts_root
    )
    contents = (
        json.dumps(_mk_details("p1")) + "\n"
        + "{not valid json\n"
        + json.dumps(_mk_details("p2")) + "\n"
    )
    drive.add_file_to_folder(
        city_folder, "20260429-100000-0001.jsonl", contents,
        mime_type="application/x-ndjson",
    )
    ctrl = BootstrapController(drive=drive, repo=repo, artifacts_dir=tmp_path / "artifacts")

    result = ctrl.bootstrap_city("Ludhiana")

    assert result.n_lines_total == 3
    assert result.n_lines_failed == 1
    assert result.n_leads_upserted == 2
    assert repo.exists("p1")
    assert repo.exists("p2")


def test_bootstrap_ignores_non_jsonl_files_in_drive_folder(
    drive, repo, tmp_path
):
    """Defensive: if someone drops a random file in the artifacts folder,
    bootstrap should skip it rather than try to parse it."""
    artifacts_root = drive.find_or_create_subfolder(ARTIFACTS_DRIVE_FOLDER)
    city_folder = drive.find_or_create_subfolder(
        "ludhiana", parent_folder_id=artifacts_root
    )
    drive.add_file_to_folder(
        city_folder, "notes.txt", "some random text", mime_type="text/plain"
    )
    drive.add_file_to_folder(
        city_folder,
        "20260429-100000-0001.jsonl",
        json.dumps(_mk_details("p1")) + "\n",
    )
    ctrl = BootstrapController(drive=drive, repo=repo, artifacts_dir=tmp_path / "artifacts")

    result = ctrl.bootstrap_city("Ludhiana")

    assert result.n_drive_artifacts == 1  # only the .jsonl counted
    assert result.n_leads_upserted == 1


# ── _parse_run_id_timestamp helper ─────────────────────────────────────


def test_parse_run_id_timestamp_normal():
    assert _parse_run_id_timestamp("20260429-125652-ad4e.jsonl") == datetime(
        2026, 4, 29, 12, 56, 52, tzinfo=timezone.utc
    )


def test_parse_run_id_timestamp_no_extension():
    assert _parse_run_id_timestamp("20260429-125652-ad4e") == datetime(
        2026, 4, 29, 12, 56, 52, tzinfo=timezone.utc
    )


def test_parse_run_id_timestamp_invalid_returns_none():
    assert _parse_run_id_timestamp("weird.jsonl") is None
    assert _parse_run_id_timestamp("not-a-timestamp.jsonl") is None
    assert _parse_run_id_timestamp("99991399-999999-zzzz.jsonl") is None
    assert _parse_run_id_timestamp("") is None
