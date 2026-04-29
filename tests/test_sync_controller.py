from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from zelda.controllers.sync import (
    ARTIFACTS_DRIVE_FOLDER,
    RAW_LEAD_SHEET_HEADER,
    DriveSyncController,
    sheet_name_for_city,
)
from zelda.models.raw_lead import RawLead
from zelda.repositories.raw_lead_repo import RawLeadRepository


_T1 = datetime(2026, 4, 29, 10, 0, 0, tzinfo=timezone.utc)
_T2 = _T1 + timedelta(hours=1)
_T3 = _T1 + timedelta(hours=2)


# ── fakes ────────────────────────────────────────────────────────────────


class FakeDriveGateway:
    """Stand-in for GoogleDriveGateway. Tracks calls and emulates the
    folder/sheet/file world in memory."""

    def __init__(self, root_folder_id: str = "root") -> None:
        self.root_folder_id = root_folder_id
        # (parent_id, name) → folder_id
        self._folders: dict[tuple[str, str], str] = {}
        # (parent_id, name) → sheet_id
        self._sheets: dict[tuple[str, str], str] = {}
        # folder_id → list of {id, name, mimeType}
        self._files_in_folder: dict[str, list[dict[str, str]]] = {root_folder_id: []}
        # call records
        self.upsert_calls: list[dict[str, Any]] = []
        self.upload_calls: list[dict[str, Any]] = []
        self._next_id = 0

    def _new_id(self, prefix: str) -> str:
        self._next_id += 1
        return f"{prefix}-{self._next_id}"

    def find_or_create_subfolder(
        self, name: str, *, parent_folder_id: str | None = None
    ) -> str:
        parent = parent_folder_id or self.root_folder_id
        key = (parent, name)
        if key in self._folders:
            return self._folders[key]
        new_id = self._new_id("folder")
        self._folders[key] = new_id
        self._files_in_folder.setdefault(parent, []).append(
            {"id": new_id, "name": name, "mimeType": "application/vnd.google-apps.folder"}
        )
        self._files_in_folder.setdefault(new_id, [])
        return new_id

    def find_or_create_spreadsheet(
        self, name: str, *, parent_folder_id: str | None = None
    ) -> str:
        parent = parent_folder_id or self.root_folder_id
        key = (parent, name)
        if key in self._sheets:
            return self._sheets[key]
        new_id = self._new_id("sheet")
        self._sheets[key] = new_id
        self._files_in_folder.setdefault(parent, []).append(
            {"id": new_id, "name": name, "mimeType": "application/vnd.google-apps.spreadsheet"}
        )
        return new_id

    def upsert_sheet_rows_by_key(
        self,
        spreadsheet_id: str,
        *,
        header: list[str],
        rows: list[dict],
        key_column: str,
    ) -> dict[str, int]:
        rows = list(rows)
        self.upsert_calls.append(
            {
                "spreadsheet_id": spreadsheet_id,
                "header": list(header),
                "rows": rows,
                "key_column": key_column,
            }
        )
        # Naive: the fake doesn't track sheet contents; treat all as insert
        return {"inserted": len(rows), "updated": 0}

    def upload_file(
        self,
        local_path: Path | str,
        *,
        parent_folder_id: str | None = None,
        drive_name: str | None = None,
        mime_type: str = "application/octet-stream",
    ) -> str:
        parent = parent_folder_id or self.root_folder_id
        name = drive_name or Path(local_path).name
        new_id = self._new_id("file")
        self._files_in_folder.setdefault(parent, []).append(
            {"id": new_id, "name": name, "mimeType": mime_type}
        )
        self.upload_calls.append(
            {
                "local_path": str(local_path),
                "parent_folder_id": parent,
                "drive_name": name,
                "mime_type": mime_type,
            }
        )
        return new_id

    def list_files_in_folder(
        self, folder_id: str, *, mime_type: str | None = None
    ) -> list[dict]:
        files = self._files_in_folder.get(folder_id, [])
        if mime_type:
            files = [f for f in files if f.get("mimeType") == mime_type]
        return list(files)


# ── helpers + fixtures ───────────────────────────────────────────────────


def _mk_lead(
    place_id: str = "p1",
    city: str = "Ludhiana",
    *,
    last_modified_at: datetime = _T1,
    last_synced_at: datetime | None = None,
    **overrides: Any,
) -> RawLead:
    base: dict[str, Any] = dict(
        place_id=place_id,
        city=city,
        name=f"Clinic {place_id}",
        discovered_at=last_modified_at,
        last_modified_at=last_modified_at,
        last_synced_at=last_synced_at,
        rating=4.5,
    )
    base.update(overrides)
    return RawLead(**base)


@pytest.fixture
def drive() -> FakeDriveGateway:
    return FakeDriveGateway()


@pytest.fixture
def repo():
    r = RawLeadRepository(":memory:")
    yield r
    r.close()


@pytest.fixture
def controller(drive: FakeDriveGateway, repo: RawLeadRepository, tmp_path: Path) -> DriveSyncController:
    return DriveSyncController(drive=drive, repo=repo, artifacts_dir=tmp_path / "artifacts")


# ── no-op cases ─────────────────────────────────────────────────────────


def test_sync_with_no_data_is_noop(controller, drive):
    result = controller.sync_city("Ludhiana")
    assert result.n_unsynced == 0
    assert result.sheet_id is None
    assert drive.upsert_calls == []
    assert drive.upload_calls == []


def test_sync_rejects_blank_city(controller):
    with pytest.raises(ValueError, match="city"):
        controller.sync_city("")
    with pytest.raises(ValueError, match="city"):
        controller.sync_city("   ")


# ── sheet sync ──────────────────────────────────────────────────────────


def test_sync_creates_sheet_with_canonical_name(controller, drive, repo):
    repo.upsert_many([_mk_lead("p1")])

    result = controller.sync_city("Ludhiana")

    assert result.sheet_name == "Zelda — Raw Leads — Dentists — Ludhiana"
    assert result.sheet_id is not None
    assert len(drive.upsert_calls) == 1


def test_sync_writes_unsynced_rows_to_sheet(controller, drive, repo):
    repo.upsert_many(
        [
            _mk_lead("p1", rating=4.5),
            _mk_lead("p2", rating=3.9),
        ]
    )

    result = controller.sync_city("Ludhiana")

    assert result.n_unsynced == 2
    upsert_call = drive.upsert_calls[0]
    assert upsert_call["header"] == RAW_LEAD_SHEET_HEADER
    assert upsert_call["key_column"] == "place_id"
    sent_ids = {row["place_id"] for row in upsert_call["rows"]}
    assert sent_ids == {"p1", "p2"}


def test_sync_marks_leads_as_synced_in_repo(controller, drive, repo):
    repo.upsert_many([_mk_lead("p1"), _mk_lead("p2")])
    assert repo.count_unsynced_for_city("Ludhiana") == 2

    controller.sync_city("Ludhiana")

    assert repo.count_unsynced_for_city("Ludhiana") == 0
    lead = repo.get_by_id("p1")
    assert lead is not None
    assert lead.last_synced_at is not None


def test_second_sync_with_no_changes_is_a_noop(controller, drive, repo):
    repo.upsert_many([_mk_lead("p1")])
    controller.sync_city("Ludhiana")
    drive.upsert_calls.clear()

    result2 = controller.sync_city("Ludhiana")

    assert result2.n_unsynced == 0
    assert drive.upsert_calls == []  # no second push


def test_modification_after_sync_is_picked_up(drive, repo, tmp_path):
    """Full delta-detection contract end-to-end through sync.
    Uses a fixed clock so we can assert against precise timestamps."""
    fixed_time = [_T2]  # mutable so we can advance it between calls
    ctrl = DriveSyncController(
        drive=drive,
        repo=repo,
        artifacts_dir=tmp_path / "artifacts",
        clock=lambda: fixed_time[0],
    )

    # Insert a lead modified at _T1 (before our first sync at _T2).
    repo.upsert_many([_mk_lead("p1", last_modified_at=_T1, rating=4.0)])
    ctrl.sync_city("Ludhiana")  # marks last_synced_at = _T2
    drive.upsert_calls.clear()

    # Now advance the clock to _T3 (after _T2) and modify the lead with
    # last_modified_at=_T3 — strictly later than the previous sync time.
    fixed_time[0] = _T3
    repo.upsert_many([_mk_lead("p1", last_modified_at=_T3, rating=4.6)])
    result = ctrl.sync_city("Ludhiana")

    assert result.n_unsynced == 1
    assert len(drive.upsert_calls) == 1
    sent = drive.upsert_calls[0]["rows"][0]
    assert sent["rating"] == 4.6


def test_sync_writes_sync_time_into_last_synced_at_cell(controller, drive, repo):
    """The value sent to the sheet for last_synced_at must equal what we
    persist to the DB — a single sync_time used for both."""
    repo.upsert_many([_mk_lead("p1")])
    controller.sync_city("Ludhiana")

    sent_row = drive.upsert_calls[0]["rows"][0]
    db_row = repo.get_by_id("p1")
    assert db_row is not None
    assert db_row.last_synced_at is not None
    # The sheet row's last_synced_at should be the same datetime as the
    # DB's last_synced_at.
    assert sent_row["last_synced_at"] == db_row.last_synced_at


def test_sync_filters_by_city(controller, drive, repo):
    repo.upsert_many(
        [
            _mk_lead("p1", city="Ludhiana"),
            _mk_lead("p2", city="Mumbai"),
        ]
    )

    controller.sync_city("Ludhiana")

    sent_ids = {row["place_id"] for row in drive.upsert_calls[0]["rows"]}
    assert sent_ids == {"p1"}
    # Mumbai lead untouched
    mumbai_lead = repo.get_by_id("p2")
    assert mumbai_lead is not None
    assert mumbai_lead.last_synced_at is None


# ── error handling ──────────────────────────────────────────────────────


def test_sheet_failure_does_not_mark_synced(controller, drive, repo):
    """If the sheet upsert raises, we MUST NOT mark rows as synced —
    next run should retry. At-least-once delivery."""
    def explode(*a, **kw):
        raise RuntimeError("Drive 503")

    drive.upsert_sheet_rows_by_key = explode  # type: ignore
    repo.upsert_many([_mk_lead("p1")])

    result = controller.sync_city("Ludhiana")

    assert any("sheet sync failed" in e for e in result.errors)
    # The lead should still be unsynced
    assert repo.count_unsynced_for_city("Ludhiana") == 1
    lead = repo.get_by_id("p1")
    assert lead is not None
    assert lead.last_synced_at is None


# ── artifact sync ───────────────────────────────────────────────────────


def test_no_artifacts_dir_is_safe(controller, drive, repo, tmp_path):
    """Sync should not blow up when the city's artifacts dir doesn't
    even exist (could happen if discovery never ran for this city or
    only synced sheet-only)."""
    repo.upsert_many([_mk_lead("p1")])

    result = controller.sync_city("Ludhiana")

    assert result.n_artifacts_uploaded == 0
    assert result.n_artifacts_skipped == 0


def test_sync_uploads_local_artifacts_to_per_city_subfolder(
    drive, repo, tmp_path
):
    artifacts_dir = tmp_path / "artifacts"
    city_dir = artifacts_dir / "ludhiana"
    city_dir.mkdir(parents=True)
    (city_dir / "run-001.jsonl").write_text('{"id":"p1"}\n')
    (city_dir / "run-002.jsonl").write_text('{"id":"p2"}\n')

    controller = DriveSyncController(drive=drive, repo=repo, artifacts_dir=artifacts_dir)
    repo.upsert_many([_mk_lead("p1")])

    result = controller.sync_city("Ludhiana")

    assert result.n_artifacts_uploaded == 2
    assert result.n_artifacts_skipped == 0
    assert len(drive.upload_calls) == 2
    uploaded_names = {c["drive_name"] for c in drive.upload_calls}
    assert uploaded_names == {"run-001.jsonl", "run-002.jsonl"}
    # All went to the same per-city folder under raw-artifacts/
    parents = {c["parent_folder_id"] for c in drive.upload_calls}
    assert len(parents) == 1


def test_sync_skips_artifacts_already_on_drive(drive, repo, tmp_path):
    artifacts_dir = tmp_path / "artifacts"
    city_dir = artifacts_dir / "ludhiana"
    city_dir.mkdir(parents=True)
    (city_dir / "run-001.jsonl").write_text('{"id":"p1"}\n')
    (city_dir / "run-002.jsonl").write_text('{"id":"p2"}\n')

    # Pre-seed Drive with run-001 already there
    artifacts_root = drive.find_or_create_subfolder(ARTIFACTS_DRIVE_FOLDER)
    city_folder = drive.find_or_create_subfolder(
        "ludhiana", parent_folder_id=artifacts_root
    )
    drive._files_in_folder[city_folder].append(
        {"id": "preexisting", "name": "run-001.jsonl", "mimeType": "application/x-ndjson"}
    )

    controller = DriveSyncController(drive=drive, repo=repo, artifacts_dir=artifacts_dir)
    result = controller.sync_city("Ludhiana")

    assert result.n_artifacts_uploaded == 1
    assert result.n_artifacts_skipped == 1
    uploaded_names = {c["drive_name"] for c in drive.upload_calls}
    assert uploaded_names == {"run-002.jsonl"}


def test_sync_uploads_artifacts_even_when_no_unsynced_rows(
    drive, repo, tmp_path
):
    """Edge case: unsynced rows is 0, but a previous sheet-sync didn't
    upload an artifact (e.g. it was added manually). Sync should still
    pick up the artifact."""
    artifacts_dir = tmp_path / "artifacts"
    city_dir = artifacts_dir / "ludhiana"
    city_dir.mkdir(parents=True)
    (city_dir / "run-001.jsonl").write_text('{"id":"p1"}\n')

    controller = DriveSyncController(drive=drive, repo=repo, artifacts_dir=artifacts_dir)
    result = controller.sync_city("Ludhiana")

    assert result.n_unsynced == 0
    assert result.sheet_id is None  # no sheet created
    assert result.n_artifacts_uploaded == 1


# ── helper ──────────────────────────────────────────────────────────────


def test_sheet_name_for_city_uses_canonical_format():
    assert sheet_name_for_city("Ludhiana") == "Zelda — Raw Leads — Dentists — Ludhiana"
    assert sheet_name_for_city("New Delhi") == "Zelda — Raw Leads — Dentists — New Delhi"


# ── header sanity ───────────────────────────────────────────────────────


def test_raw_lead_sheet_header_has_no_duplicates():
    assert len(RAW_LEAD_SHEET_HEADER) == len(set(RAW_LEAD_SHEET_HEADER))


def test_raw_lead_sheet_header_includes_place_id_first():
    """place_id is the upsert key — having it first makes scans
    eyeball-able."""
    assert RAW_LEAD_SHEET_HEADER[0] == "place_id"
