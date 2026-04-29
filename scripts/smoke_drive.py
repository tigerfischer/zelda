"""Manual smoke test for the Drive + Sheets gateway.

Hits the real Google Drive (the Zelva folder) and exercises:
1. Subfolder find-or-create (idempotent)
2. Spreadsheet find-or-create (idempotent)
3. Sheet upsert (initial + with updates + with appends)
4. Sheet read-back (round-trip)
5. File upload + list + download
6. Cleanup (delete the test subfolder, trashing all contents)

    conda run -n zelda python scripts/smoke_drive.py

The script writes everything inside a uniquely-named test subfolder so
it can clean up afterwards without touching real Zelda data. Cost: $0
(Drive operations are free for our volumes).
"""

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from zelda.config import Settings
from zelda.gateways.google_drive import GoogleDriveGateway


def main() -> int:
    settings = Settings()
    gw = GoogleDriveGateway.from_oauth_file(
        settings.google_oauth_client_secrets,
        settings.google_oauth_token_cache,
        settings.google_drive_folder_id,
    )

    test_folder_name = (
        f"__zelda-smoke-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}__"
    )
    print(f"Creating ephemeral test folder: {test_folder_name}")
    test_folder_id = gw.find_or_create_subfolder(test_folder_name)
    print(f"  → folder_id = {test_folder_id}")

    try:
        # 1. Idempotency: calling find_or_create_subfolder again must
        # return the same ID.
        print("\nIdempotency check on subfolder…")
        again = gw.find_or_create_subfolder(test_folder_name)
        assert again == test_folder_id, "subfolder find_or_create not idempotent"
        print("  ✓ idempotent")

        # 2. Spreadsheet creation inside the test folder.
        sheet_name = "Smoke Sheet — Dentists — Ludhiana"
        print(f"\nCreating spreadsheet: {sheet_name}")
        sheet_id = gw.find_or_create_spreadsheet(
            sheet_name, parent_folder_id=test_folder_id
        )
        print(f"  → sheet_id = {sheet_id}")

        again_sheet = gw.find_or_create_spreadsheet(
            sheet_name, parent_folder_id=test_folder_id
        )
        assert again_sheet == sheet_id, "spreadsheet find_or_create not idempotent"
        print("  ✓ idempotent")

        # 3. Initial upsert into empty sheet.
        header = ["place_id", "name", "rating", "review_count", "city"]
        rows_v1 = [
            {
                "place_id": "p1",
                "name": "Sai Dental",
                "rating": 4.9,
                "review_count": 272,
                "city": "Ludhiana",
            },
            {
                "place_id": "p2",
                "name": "Saggar Dental",
                "rating": 5.0,
                "review_count": 727,
                "city": "Ludhiana",
            },
        ]
        print("\nInitial upsert (2 new rows)…")
        result = gw.upsert_sheet_rows_by_key(
            sheet_id, header=header, rows=rows_v1, key_column="place_id"
        )
        print(f"  → {result}")
        assert result == {"inserted": 2, "updated": 0}

        readback = gw.read_all_sheet_rows(sheet_id)
        assert len(readback) == 2
        assert {r["place_id"] for r in readback} == {"p1", "p2"}
        print(f"  ✓ readback OK ({len(readback)} rows)")

        # 4. Mixed upsert: update p1, append p3.
        rows_v2 = [
            {
                "place_id": "p1",
                "name": "Sai Dental UPDATED",
                "rating": 4.95,
                "review_count": 290,
                "city": "Ludhiana",
            },
            {
                "place_id": "p3",
                "name": "Foo Clinic",
                "rating": 4.2,
                "review_count": 50,
                "city": "Ludhiana",
            },
        ]
        print("\nMixed upsert (1 update + 1 append)…")
        result = gw.upsert_sheet_rows_by_key(
            sheet_id, header=header, rows=rows_v2, key_column="place_id"
        )
        print(f"  → {result}")
        assert result == {"inserted": 1, "updated": 1}

        readback = gw.read_all_sheet_rows(sheet_id)
        assert len(readback) == 3
        rows_by_id = {r["place_id"]: r for r in readback}
        assert rows_by_id["p1"]["name"] == "Sai Dental UPDATED"
        assert rows_by_id["p2"]["name"] == "Saggar Dental"
        assert rows_by_id["p3"]["name"] == "Foo Clinic"
        print("  ✓ p1 updated, p2 untouched, p3 appended")

        # 5. File upload.
        print("\nUploading a small JSONL artifact…")
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        ) as tf:
            for i in range(3):
                tf.write(json.dumps({"id": f"p{i}", "test": True}) + "\n")
            tmp_path = Path(tf.name)
        try:
            uploaded_id = gw.upload_file(
                tmp_path,
                parent_folder_id=test_folder_id,
                drive_name="smoke-artifact.jsonl",
                mime_type="application/x-ndjson",
            )
            print(f"  → file_id = {uploaded_id}")

            # 6. List folder; expect the spreadsheet + the JSONL file.
            print("\nListing test folder contents…")
            entries = gw.list_files_in_folder(test_folder_id)
            names = sorted(e["name"] for e in entries)
            print(f"  → {names}")
            assert "smoke-artifact.jsonl" in names
            assert sheet_name in names

            # 7. Download the file back, compare bytes.
            print("\nDownloading the uploaded file back…")
            with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as out:
                out_path = Path(out.name)
            try:
                gw.download_file(uploaded_id, out_path)
                round_trip = out_path.read_bytes()
                original = tmp_path.read_bytes()
                assert round_trip == original, "round-tripped bytes differ"
                print("  ✓ bytes round-tripped exactly")
            finally:
                out_path.unlink(missing_ok=True)
        finally:
            tmp_path.unlink(missing_ok=True)

        print("\nAll Drive smoke checks passed ✓")
        return 0

    finally:
        # Cleanup: trash the test folder. Drive cascades the trash to
        # children, so the spreadsheet + uploaded file go with it.
        print(f"\nCleaning up: deleting test folder {test_folder_id}")
        try:
            gw.delete_file(test_folder_id)
            print("  ✓ deleted")
        except Exception as e:
            print(f"  ⚠ cleanup failed: {e}")


if __name__ == "__main__":
    sys.exit(main())
