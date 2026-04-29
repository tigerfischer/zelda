import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, call

import pytest

from zelda.gateways.google_drive import (
    GoogleDriveGateway,
    _row_dict_to_list,
    _to_cell_value,
)


# ── fixtures ────────────────────────────────────────────────────────────


def _gateway_with_mocks(
    *,
    list_response: dict | None = None,
    create_response: dict | None = None,
    get_all_values: list[list[str]] | None = None,
) -> tuple[GoogleDriveGateway, MagicMock, MagicMock, MagicMock]:
    """Build a gateway with mocks suitable for the common cases.

    Returns: (gateway, drive_client, gspread_client, worksheet_mock).

    `list_response` is what `drive.files().list().execute()` returns.
    `create_response` is what `drive.files().create().execute()` returns.
    `get_all_values` is what gspread `worksheet.get_all_values()` returns.
    """
    drive = MagicMock()
    if list_response is not None:
        drive.files.return_value.list.return_value.execute.return_value = list_response
    if create_response is not None:
        drive.files.return_value.create.return_value.execute.return_value = (
            create_response
        )

    worksheet = MagicMock()
    if get_all_values is not None:
        worksheet.get_all_values.return_value = get_all_values
    spreadsheet = MagicMock(sheet1=worksheet)
    gc = MagicMock()
    gc.open_by_key.return_value = spreadsheet

    gw = GoogleDriveGateway(
        drive_client=drive,
        gspread_client=gc,
        root_folder_id="root_folder_id",
    )
    return gw, drive, gc, worksheet


# ── construction ────────────────────────────────────────────────────────


def test_constructor_rejects_blank_root_folder_id():
    with pytest.raises(ValueError, match="root_folder_id"):
        GoogleDriveGateway(
            drive_client=MagicMock(),
            gspread_client=MagicMock(),
            root_folder_id="",
        )
    with pytest.raises(ValueError, match="root_folder_id"):
        GoogleDriveGateway(
            drive_client=MagicMock(),
            gspread_client=MagicMock(),
            root_folder_id="   ",
        )


def test_root_folder_id_property_returns_trimmed_value():
    gw, _, _, _ = _gateway_with_mocks()
    assert gw.root_folder_id == "root_folder_id"


# ── find_or_create_subfolder ───────────────────────────────────────────


def test_find_or_create_subfolder_returns_existing_id_when_present():
    gw, drive, _, _ = _gateway_with_mocks(
        list_response={"files": [{"id": "existing_id", "name": "Foo"}]},
    )
    result = gw.find_or_create_subfolder("Foo")
    assert result == "existing_id"
    drive.files.return_value.create.assert_not_called()


def test_find_or_create_subfolder_creates_when_missing():
    gw, drive, _, _ = _gateway_with_mocks(
        list_response={"files": []},
        create_response={"id": "new_folder_id"},
    )
    result = gw.find_or_create_subfolder("Foo")
    assert result == "new_folder_id"
    create_call = drive.files.return_value.create.call_args
    assert create_call.kwargs["body"]["name"] == "Foo"
    assert (
        create_call.kwargs["body"]["mimeType"]
        == "application/vnd.google-apps.folder"
    )
    assert create_call.kwargs["body"]["parents"] == ["root_folder_id"]


def test_find_or_create_subfolder_uses_explicit_parent():
    gw, drive, _, _ = _gateway_with_mocks(
        list_response={"files": []},
        create_response={"id": "new_id"},
    )
    gw.find_or_create_subfolder("Foo", parent_folder_id="custom_parent")
    create_call = drive.files.return_value.create.call_args
    assert create_call.kwargs["body"]["parents"] == ["custom_parent"]


# ── find_or_create_spreadsheet ─────────────────────────────────────────


def test_find_or_create_spreadsheet_returns_existing():
    gw, drive, _, _ = _gateway_with_mocks(
        list_response={"files": [{"id": "existing_sheet_id", "name": "Foo"}]},
    )
    assert gw.find_or_create_spreadsheet("Foo") == "existing_sheet_id"
    drive.files.return_value.create.assert_not_called()


def test_find_or_create_spreadsheet_creates_when_missing():
    gw, drive, _, _ = _gateway_with_mocks(
        list_response={"files": []},
        create_response={"id": "new_sheet_id"},
    )
    result = gw.find_or_create_spreadsheet("Zelda — Raw Leads — Dentists — Ludhiana")
    assert result == "new_sheet_id"
    create_call = drive.files.return_value.create.call_args
    assert (
        create_call.kwargs["body"]["mimeType"]
        == "application/vnd.google-apps.spreadsheet"
    )


# ── upsert_sheet_rows_by_key ───────────────────────────────────────────


_HEADER = ["place_id", "name", "rating"]


def test_upsert_rejects_key_column_not_in_header():
    gw, _, _, _ = _gateway_with_mocks(get_all_values=[])
    with pytest.raises(ValueError, match="key_column"):
        gw.upsert_sheet_rows_by_key(
            "sheet_id",
            header=_HEADER,
            rows=[],
            key_column="missing",
        )


def test_upsert_initializes_empty_sheet_with_header_and_rows():
    gw, _, _, ws = _gateway_with_mocks(get_all_values=[])
    rows = [
        {"place_id": "p1", "name": "Foo", "rating": 4.5},
        {"place_id": "p2", "name": "Bar", "rating": 4.0},
    ]
    result = gw.upsert_sheet_rows_by_key(
        "sheet_id", header=_HEADER, rows=rows, key_column="place_id"
    )
    assert result == {"inserted": 2, "updated": 0}
    ws.update.assert_called_once()
    call_kwargs = ws.update.call_args.kwargs
    assert call_kwargs["range_name"] == "A1"
    written = call_kwargs["values"]
    assert written[0] == _HEADER
    assert written[1] == ["p1", "Foo", 4.5]
    assert written[2] == ["p2", "Bar", 4.0]


def test_upsert_updates_existing_row_and_appends_new():
    """Mixed batch: one row matches an existing place_id (update), the
    other is brand new (append)."""
    existing = [
        ["place_id", "name", "rating"],
        ["p1", "Foo Old", "4.0"],
    ]
    gw, _, _, ws = _gateway_with_mocks(get_all_values=existing)

    rows = [
        {"place_id": "p1", "name": "Foo NEW", "rating": 4.7},  # update
        {"place_id": "p2", "name": "Bar", "rating": 4.2},      # append
    ]
    result = gw.upsert_sheet_rows_by_key(
        "sheet_id", header=_HEADER, rows=rows, key_column="place_id"
    )
    assert result == {"inserted": 1, "updated": 1}

    # Update path: batch_update was called with the p1 row
    ws.batch_update.assert_called_once()
    update_payload = ws.batch_update.call_args.args[0]
    assert len(update_payload) == 1
    assert update_payload[0]["range"] == "A2:C2"
    assert update_payload[0]["values"] == [["p1", "Foo NEW", 4.7]]

    # Append path: append_rows was called with the p2 row
    ws.append_rows.assert_called_once()
    append_payload = ws.append_rows.call_args.args[0]
    assert append_payload == [["p2", "Bar", 4.2]]


def test_upsert_skips_rows_missing_the_key_value():
    gw, _, _, ws = _gateway_with_mocks(
        get_all_values=[["place_id", "name", "rating"]]
    )
    rows = [
        {"place_id": "p1", "name": "Foo", "rating": 4.5},
        {"name": "No key", "rating": 4.0},  # missing place_id
    ]
    result = gw.upsert_sheet_rows_by_key(
        "sheet_id", header=_HEADER, rows=rows, key_column="place_id"
    )
    assert result == {"inserted": 1, "updated": 0}


def test_upsert_rewrites_header_when_drifted():
    """If the existing header row in Sheets differs from the header we're
    writing, rewrite the header so column positions align."""
    drifted = [["wrong", "header", "labels"], ["p1", "Foo", "4.0"]]
    gw, _, _, ws = _gateway_with_mocks(get_all_values=drifted)

    gw.upsert_sheet_rows_by_key(
        "sheet_id",
        header=_HEADER,
        rows=[{"place_id": "p1", "name": "Foo NEW", "rating": 4.5}],
        key_column="place_id",
    )

    # First update.kwargs call must be the header rewrite.
    header_call = ws.update.call_args_list[0]
    assert header_call.kwargs["values"] == [_HEADER]
    assert header_call.kwargs["range_name"] == "A1:C1"


# ── read_all_sheet_rows ────────────────────────────────────────────────


def test_read_all_sheet_rows_returns_dicts_keyed_by_header():
    gw, _, _, _ = _gateway_with_mocks(
        get_all_values=[
            ["place_id", "name", "rating"],
            ["p1", "Foo", "4.5"],
            ["p2", "Bar", "4.0"],
        ],
    )
    out = gw.read_all_sheet_rows("sheet_id")
    assert out == [
        {"place_id": "p1", "name": "Foo", "rating": "4.5"},
        {"place_id": "p2", "name": "Bar", "rating": "4.0"},
    ]


def test_read_all_sheet_rows_returns_empty_for_empty_sheet():
    gw, _, _, _ = _gateway_with_mocks(get_all_values=[])
    assert gw.read_all_sheet_rows("sheet_id") == []


def test_read_all_sheet_rows_returns_empty_for_header_only_sheet():
    gw, _, _, _ = _gateway_with_mocks(
        get_all_values=[["place_id", "name"]]
    )
    assert gw.read_all_sheet_rows("sheet_id") == []


def test_read_all_sheet_rows_pads_short_rows_with_empty_strings():
    """If a sheet row has fewer cells than the header (e.g. trailing
    empty cells stripped by Sheets), short rows should pad with empty
    strings so the dict shape is consistent."""
    gw, _, _, _ = _gateway_with_mocks(
        get_all_values=[
            ["place_id", "name", "rating"],
            ["p1", "Foo"],  # missing rating
        ],
    )
    out = gw.read_all_sheet_rows("sheet_id")
    assert out == [{"place_id": "p1", "name": "Foo", "rating": ""}]


# ── upload_file ────────────────────────────────────────────────────────


def test_upload_file_creates_with_correct_parent_and_name(tmp_path):
    f = tmp_path / "x.jsonl"
    f.write_text('{"a": 1}\n')
    gw, drive, _, _ = _gateway_with_mocks(create_response={"id": "uploaded_id"})

    result = gw.upload_file(f, drive_name="custom.jsonl")
    assert result == "uploaded_id"
    create_call = drive.files.return_value.create.call_args
    assert create_call.kwargs["body"]["name"] == "custom.jsonl"
    assert create_call.kwargs["body"]["parents"] == ["root_folder_id"]


def test_upload_file_raises_when_local_path_missing(tmp_path):
    gw, _, _, _ = _gateway_with_mocks()
    with pytest.raises(FileNotFoundError):
        gw.upload_file(tmp_path / "does-not-exist.jsonl")


# ── list_files_in_folder ───────────────────────────────────────────────


def test_list_files_in_folder_paginates():
    drive = MagicMock()
    page1 = {
        "files": [{"id": "a", "name": "a.jsonl"}],
        "nextPageToken": "tok1",
    }
    page2 = {"files": [{"id": "b", "name": "b.jsonl"}]}
    drive.files.return_value.list.return_value.execute.side_effect = [
        page1,
        page2,
    ]
    gw = GoogleDriveGateway(
        drive_client=drive, gspread_client=MagicMock(), root_folder_id="root"
    )
    result = gw.list_files_in_folder("folder_id")
    assert [r["id"] for r in result] == ["a", "b"]


# ── _to_cell_value coercion ────────────────────────────────────────────


def test_to_cell_value_none_to_empty_string():
    assert _to_cell_value(None) == ""


def test_to_cell_value_passes_scalars():
    assert _to_cell_value(42) == 42
    assert _to_cell_value(4.5) == 4.5
    assert _to_cell_value("hello") == "hello"
    assert _to_cell_value(True) is True


def test_to_cell_value_dict_to_json_string():
    assert _to_cell_value({"a": 1}) == '{"a": 1}'


def test_to_cell_value_list_to_json_string():
    assert _to_cell_value([1, 2, 3]) == "[1, 2, 3]"


def test_to_cell_value_datetime_to_isoformat():
    dt = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
    out = _to_cell_value(dt)
    assert out.startswith("2026-04-29T12:00:00")


def test_to_cell_value_unicode_preserved_in_json():
    assert _to_cell_value({"name": "𝗦𝗮𝗶 Dental"}) == json.dumps(
        {"name": "𝗦𝗮𝗶 Dental"}, ensure_ascii=False
    )


# ── _row_dict_to_list ──────────────────────────────────────────────────


def test_row_dict_to_list_aligns_to_header_order():
    out = _row_dict_to_list(
        {"name": "Foo", "rating": 4.5, "place_id": "p1"},
        ["place_id", "name", "rating"],
    )
    assert out == ["p1", "Foo", 4.5]


def test_row_dict_to_list_fills_missing_with_empty_string():
    out = _row_dict_to_list({"place_id": "p1"}, ["place_id", "name", "rating"])
    assert out == ["p1", "", ""]
