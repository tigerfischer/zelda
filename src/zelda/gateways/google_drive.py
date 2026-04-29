"""Google Drive + Sheets gateway.

Wraps two Google APIs through one class:
- Drive API v3 (via google-api-python-client) for folder/file/spreadsheet
  management — find_or_create folders + spreadsheets, upload arbitrary
  files, list files, download files.
- Sheets via gspread for read/write of cell values.

Authenticates via a service account JSON file.

Design notes
------------
- Construction is via dependency injection (`drive_client` and
  `gspread_client`) so unit tests can pass mocks. Use the
  `from_service_account_file` classmethod in production.
- All public methods accept an explicit `parent_folder_id` where
  relevant; `None` means "use the gateway's `root_folder_id`".
- Sheet upserts preserve any user-added columns to the right of the
  declared header — we only ever overwrite the columns we own.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import gspread
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from gspread.utils import rowcol_to_a1
from loguru import logger


_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
)

_FOLDER_MIME = "application/vnd.google-apps.folder"
_SHEET_MIME = "application/vnd.google-apps.spreadsheet"


class GoogleDriveError(Exception):
    """Raised on Drive/Sheets failures the gateway can't recover from."""


class GoogleDriveGateway:
    """Wrapper over Google Drive + Sheets, scoped to one root folder."""

    def __init__(
        self,
        *,
        drive_client: Any,
        gspread_client: Any,
        root_folder_id: str,
    ) -> None:
        if not root_folder_id or not root_folder_id.strip():
            raise ValueError("root_folder_id must be non-empty")
        self._drive = drive_client
        self._gspread = gspread_client
        self._root_folder_id = root_folder_id.strip()

    @classmethod
    def from_service_account_file(
        cls,
        credentials_path: Path | str,
        root_folder_id: str,
    ) -> "GoogleDriveGateway":
        """Build via a service account JSON key.

        Note: service accounts have 0 GB of personal storage. They can
        only create files when the target folder lives in a Google
        Workspace Shared Drive (where storage is pooled). For personal
        Google accounts, prefer `from_oauth_file`.
        """
        creds = service_account.Credentials.from_service_account_file(
            str(credentials_path),
            scopes=list(_SCOPES),
        )
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        gspread_client = gspread.authorize(creds)
        return cls(
            drive_client=drive,
            gspread_client=gspread_client,
            root_folder_id=root_folder_id,
        )

    @classmethod
    def from_oauth_file(
        cls,
        client_secrets_path: Path | str,
        token_cache_path: Path | str,
        root_folder_id: str,
    ) -> "GoogleDriveGateway":
        """Build via OAuth user credentials (the script acts as the user).

        Files end up in the user's personal Drive, charged against the
        user's free 15 GB quota — no Workspace required.

        On first call: opens a browser for the user to authorize, then
        caches a refresh token at `token_cache_path`. Subsequent calls
        reuse the cached token (refreshing the access token silently).
        """
        creds = _load_or_authorize_oauth_creds(
            Path(client_secrets_path), Path(token_cache_path)
        )
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        gspread_client = gspread.authorize(creds)
        return cls(
            drive_client=drive,
            gspread_client=gspread_client,
            root_folder_id=root_folder_id,
        )

    @property
    def root_folder_id(self) -> str:
        return self._root_folder_id

    # ── folders ──────────────────────────────────────────────────────

    def find_or_create_subfolder(
        self,
        name: str,
        *,
        parent_folder_id: str | None = None,
    ) -> str:
        """Returns the ID of the subfolder named `name` under `parent_folder_id`
        (or root). Creates it if it doesn't exist. Idempotent."""
        parent = parent_folder_id or self._root_folder_id
        existing = self._find_in_folder(name, parent, mime_type=_FOLDER_MIME)
        if existing is not None:
            return existing
        body = {"name": name, "mimeType": _FOLDER_MIME, "parents": [parent]}
        result = (
            self._drive.files()
            .create(body=body, fields="id", supportsAllDrives=True)
            .execute()
        )
        folder_id = result["id"]
        logger.info(
            "drive.folder_created name={name!r} id={id} parent={parent}",
            name=name,
            id=folder_id,
            parent=parent,
        )
        return folder_id

    # ── spreadsheets ─────────────────────────────────────────────────

    def find_or_create_spreadsheet(
        self,
        name: str,
        *,
        parent_folder_id: str | None = None,
    ) -> str:
        """Returns the ID of the spreadsheet named `name` under
        `parent_folder_id` (or root). Creates it if absent. Idempotent."""
        parent = parent_folder_id or self._root_folder_id
        existing = self._find_in_folder(name, parent, mime_type=_SHEET_MIME)
        if existing is not None:
            return existing
        body = {"name": name, "mimeType": _SHEET_MIME, "parents": [parent]}
        result = (
            self._drive.files()
            .create(body=body, fields="id", supportsAllDrives=True)
            .execute()
        )
        sheet_id = result["id"]
        logger.info(
            "drive.sheet_created name={name!r} id={id} parent={parent}",
            name=name,
            id=sheet_id,
            parent=parent,
        )
        return sheet_id

    def upsert_sheet_rows_by_key(
        self,
        spreadsheet_id: str,
        *,
        header: list[str],
        rows: Iterable[dict[str, Any]],
        key_column: str,
    ) -> dict[str, int]:
        """Upsert rows in the first worksheet, keyed by `key_column`.

        - Empty sheet: writes header + all rows in one shot.
        - Existing sheet: rewrites the header row (defends against drift),
          updates rows whose `key_column` value already exists in the
          sheet, and appends those whose key is new.
        - Only touches the columns named in `header`; anything to the
          right (user-added columns) is preserved.

        Returns: `{"inserted": N, "updated": M}`.
        """
        if key_column not in header:
            raise ValueError(f"key_column {key_column!r} must be in header")

        rows = list(rows)
        ss = self._gspread.open_by_key(spreadsheet_id)
        ws = ss.sheet1

        existing = ws.get_all_values()

        if not existing:
            data = [header] + [_row_dict_to_list(r, header) for r in rows]
            ws.update(values=data, range_name="A1", value_input_option="RAW")
            logger.info(
                "drive.sheet_initialized id={id} rows={n}",
                id=spreadsheet_id,
                n=len(rows),
            )
            return {"inserted": len(rows), "updated": 0}

        existing_header = existing[0]
        if existing_header != header:
            # Header drifted (we added a column, or someone hand-edited).
            # Rewrite our header so column positions align to our `header` list.
            end_a1 = rowcol_to_a1(1, len(header))
            ws.update(
                values=[header],
                range_name=f"A1:{end_a1}",
                value_input_option="RAW",
            )

        key_idx = header.index(key_column)
        # Build place_id → 1-indexed row number (skip the header row).
        existing_keys: dict[str, int] = {}
        for i, row in enumerate(existing[1:], start=2):
            key = row[key_idx] if key_idx < len(row) else ""
            if key:
                existing_keys[key] = i

        updates: list[dict[str, Any]] = []
        appends: list[list[Any]] = []
        inserted = 0
        updated = 0

        for row_dict in rows:
            key = str(row_dict.get(key_column, "") or "")
            if not key:
                # Skip rows missing the key — would corrupt the upsert
                continue
            row_values = _row_dict_to_list(row_dict, header)
            if key in existing_keys:
                row_num = existing_keys[key]
                start = rowcol_to_a1(row_num, 1)
                end = rowcol_to_a1(row_num, len(header))
                updates.append({"range": f"{start}:{end}", "values": [row_values]})
                updated += 1
            else:
                appends.append(row_values)
                inserted += 1

        if updates:
            ws.batch_update(updates, value_input_option="RAW")
        if appends:
            ws.append_rows(appends, value_input_option="RAW")

        logger.info(
            "drive.sheet_upsert id={id} inserted={ins} updated={upd}",
            id=spreadsheet_id,
            ins=inserted,
            upd=updated,
        )
        return {"inserted": inserted, "updated": updated}

    def read_all_sheet_rows(self, spreadsheet_id: str) -> list[dict[str, Any]]:
        """Return all data rows as dicts keyed by the header row.
        Empty sheet → empty list."""
        ss = self._gspread.open_by_key(spreadsheet_id)
        ws = ss.sheet1
        existing = ws.get_all_values()
        if len(existing) < 2:
            return []
        header = existing[0]
        out: list[dict[str, Any]] = []
        for row in existing[1:]:
            # Pad short rows so dict access is consistent
            padded = list(row) + [""] * (len(header) - len(row))
            out.append({col: padded[i] for i, col in enumerate(header)})
        return out

    # ── arbitrary files ──────────────────────────────────────────────

    def upload_file(
        self,
        local_path: Path | str,
        *,
        parent_folder_id: str | None = None,
        drive_name: str | None = None,
        mime_type: str = "application/octet-stream",
    ) -> str:
        """Upload `local_path` to Drive. Always creates a new file."""
        path = Path(local_path)
        if not path.exists():
            raise FileNotFoundError(path)
        parent = parent_folder_id or self._root_folder_id
        name = drive_name or path.name
        body = {"name": name, "parents": [parent]}
        media = MediaFileUpload(str(path), mimetype=mime_type)
        result = (
            self._drive.files()
            .create(
                body=body,
                media_body=media,
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )
        file_id = result["id"]
        logger.info(
            "drive.file_uploaded name={name!r} id={id} parent={parent} bytes={bytes}",
            name=name,
            id=file_id,
            parent=parent,
            bytes=path.stat().st_size,
        )
        return file_id

    def list_files_in_folder(
        self,
        folder_id: str,
        *,
        mime_type: str | None = None,
    ) -> list[dict[str, str]]:
        """List children of `folder_id`. Returns dicts with id, name,
        mimeType, modifiedTime. Optionally filtered by mime_type."""
        query_parts = [
            f"'{folder_id}' in parents",
            "trashed = false",
        ]
        if mime_type:
            query_parts.append(f"mimeType = '{mime_type}'")
        query = " and ".join(query_parts)

        out: list[dict[str, str]] = []
        page_token: str | None = None
        while True:
            kwargs: dict[str, Any] = dict(
                q=query,
                spaces="drive",
                fields="files(id, name, mimeType, modifiedTime), nextPageToken",
                pageSize=200,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            if page_token:
                kwargs["pageToken"] = page_token
            result = self._drive.files().list(**kwargs).execute()
            out.extend(result.get("files", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                break
        return out

    def download_file(self, file_id: str, local_path: Path | str) -> Path:
        """Download a Drive file (non-Google-Doc) to `local_path`. Returns
        the path written. Used by reverse-sync to pull JSONL artifacts."""
        from googleapiclient.http import MediaIoBaseDownload
        import io

        local = Path(local_path)
        local.parent.mkdir(parents=True, exist_ok=True)
        request = self._drive.files().get_media(
            fileId=file_id, supportsAllDrives=True
        )
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        local.write_bytes(buf.getvalue())
        logger.info(
            "drive.file_downloaded id={id} path={path} bytes={bytes}",
            id=file_id,
            path=str(local),
            bytes=len(buf.getvalue()),
        )
        return local

    def delete_file(self, file_id: str) -> None:
        """Trash a file. Used by smoke tests for cleanup; not currently
        called from production code."""
        self._drive.files().delete(
            fileId=file_id, supportsAllDrives=True
        ).execute()
        logger.info("drive.file_deleted id={id}", id=file_id)

    # ── private ──────────────────────────────────────────────────────

    def _find_in_folder(
        self,
        name: str,
        parent_folder_id: str,
        *,
        mime_type: str | None = None,
    ) -> str | None:
        """Find the first file/folder by exact name within a parent.
        Returns its ID or None."""
        escaped = name.replace("\\", "\\\\").replace("'", r"\'")
        query_parts = [
            f"name = '{escaped}'",
            f"'{parent_folder_id}' in parents",
            "trashed = false",
        ]
        if mime_type:
            query_parts.append(f"mimeType = '{mime_type}'")
        query = " and ".join(query_parts)

        result = (
            self._drive.files()
            .list(
                q=query,
                spaces="drive",
                fields="files(id, name)",
                pageSize=2,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = result.get("files", [])
        if not files:
            return None
        return files[0]["id"]


# ── module-private helpers ──────────────────────────────────────────


def _load_or_authorize_oauth_creds(
    client_secrets_path: Path,
    token_cache_path: Path,
) -> Credentials:
    """Load cached OAuth user creds; refresh if expired; otherwise run
    the InstalledAppFlow (opens a browser) and cache the result.

    Idempotent across runs: only the first run on a given machine pops
    a browser. Subsequent runs reuse the refresh token silently.
    """
    if not client_secrets_path.exists():
        raise FileNotFoundError(
            f"OAuth client secrets file not found at {client_secrets_path}. "
            "Create OAuth credentials in GCP Console > APIs & Services > "
            "Credentials > Create credentials > OAuth client ID > Desktop app."
        )

    creds: Credentials | None = None
    if token_cache_path.exists():
        creds = Credentials.from_authorized_user_file(
            str(token_cache_path), list(_SCOPES)
        )

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _persist_oauth_token(creds, token_cache_path)
        return creds

    # First time on this machine — pop a browser.
    logger.info(
        "drive.oauth_first_run a browser tab will open for you to authorize "
        "Zelda; the resulting refresh token will be cached at {path}",
        path=str(token_cache_path),
    )
    flow = InstalledAppFlow.from_client_secrets_file(
        str(client_secrets_path), list(_SCOPES)
    )
    creds = flow.run_local_server(port=0)
    _persist_oauth_token(creds, token_cache_path)
    return creds


def _persist_oauth_token(creds: Credentials, token_cache_path: Path) -> None:
    token_cache_path.parent.mkdir(parents=True, exist_ok=True)
    token_cache_path.write_text(creds.to_json())


def _row_dict_to_list(row: dict[str, Any], header: list[str]) -> list[Any]:
    """Project a row dict onto the header column order, coercing each
    value to something Sheets can store."""
    return [_to_cell_value(row.get(col)) for col in header]


def _to_cell_value(v: Any) -> Any:
    """Coerce a Python value to a cell-friendly scalar."""
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float, str)):
        return v
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)
