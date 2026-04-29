"""Shared fakes used across multiple test modules.

This module is named without a `test_` prefix so pytest does not try
to collect it as a test file.
"""

from pathlib import Path
from typing import Any


class FakeDriveGateway:
    """In-memory stand-in for `GoogleDriveGateway`.

    Tracks calls so tests can assert on them, and emulates a tiny
    folder/file world so multi-step flows (upload → list → download)
    are observable end-to-end.
    """

    def __init__(self, root_folder_id: str = "root") -> None:
        self.root_folder_id = root_folder_id
        # (parent_id, name) → folder_id
        self._folders: dict[tuple[str, str], str] = {}
        # (parent_id, name) → sheet_id
        self._sheets: dict[tuple[str, str], str] = {}
        # folder_id → list of {id, name, mimeType}
        self._files_in_folder: dict[str, list[dict[str, str]]] = {root_folder_id: []}
        # file_id → bytes content (populated on upload + via add_file_to_folder)
        self._file_contents: dict[str, bytes] = {}
        # call records
        self.upsert_calls: list[dict[str, Any]] = []
        self.upload_calls: list[dict[str, Any]] = []
        self.download_calls: list[tuple[str, str]] = []
        self._next_id = 0

    def _new_id(self, prefix: str) -> str:
        self._next_id += 1
        return f"{prefix}-{self._next_id}"

    # ── test helpers (NOT part of the gateway protocol) ─────────────

    def add_file_to_folder(
        self,
        folder_id: str,
        name: str,
        contents: str | bytes,
        mime_type: str = "application/octet-stream",
    ) -> str:
        """Seed the fake with a file directly, bypassing upload_file.
        Returns the new file_id."""
        file_id = self._new_id("file")
        if isinstance(contents, str):
            contents = contents.encode("utf-8")
        self._file_contents[file_id] = contents
        self._files_in_folder.setdefault(folder_id, []).append(
            {"id": file_id, "name": name, "mimeType": mime_type}
        )
        return file_id

    # ── public gateway protocol ─────────────────────────────────────

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
        local = Path(local_path)
        if not local.exists():
            raise FileNotFoundError(local)
        name = drive_name or local.name
        new_id = self._new_id("file")
        self._file_contents[new_id] = local.read_bytes()
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

    def download_file(self, file_id: str, local_path: Path | str) -> Path:
        if file_id not in self._file_contents:
            raise FileNotFoundError(f"file_id {file_id!r} not in fake drive")
        local = Path(local_path)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(self._file_contents[file_id])
        self.download_calls.append((file_id, str(local)))
        return local
