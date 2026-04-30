"""Manual smoke test for the sync controller.

Reads the unsynced leads in `data/zelda.db` for a given city and pushes
them to Drive (the real Zelva folder). Idempotent on re-run.

    conda run -n zelda python scripts/smoke_sync.py --city Ludhiana

Cost: free (Drive operations are not metered for our volumes).

What it verifies end-to-end:
- OAuth-based Drive auth works
- The "Zelda — Raw Leads — Dentists — {City}" sheet is created or
  reused under the Zelva folder
- Unsynced rows are pushed to the sheet
- last_synced_at is stamped both in the sheet and in the DB
- JSONL artifact files for the city are mirrored under
  raw-artifacts/{slug(city)}/
- Re-running with no DB changes is a no-op (no Drive writes)
"""

import argparse
import sys

from zelda.config import Settings
from zelda.controllers.sync import DriveSyncController
from zelda.gateways.google_drive import GoogleDriveGateway
from zelda.repositories.google_places_lead_repo import GooglePlacesLeadRepository


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", default="Ludhiana", help="City to sync (default: Ludhiana)")
    args = parser.parse_args(argv)

    settings = Settings()

    print(f"Syncing {args.city!r} from {settings.db_path} → Drive folder {settings.google_drive_folder_id}")
    print(f"Artifacts dir: {settings.raw_artifacts_dir}")
    print()

    drive = GoogleDriveGateway.from_oauth_file(
        settings.google_oauth_client_secrets,
        settings.google_oauth_token_cache,
        settings.google_drive_folder_id,
    )
    repo = GooglePlacesLeadRepository(settings.db_path)
    try:
        ctrl = DriveSyncController(
            drive=drive, repo=repo, artifacts_dir=settings.raw_artifacts_dir
        )
        unsynced_before = repo.count_unsynced_for_city(args.city)
        total_before = repo.count_for_city(args.city)
        print(f"Before sync: {total_before} total leads, {unsynced_before} unsynced")

        result = ctrl.sync_city(args.city)
    finally:
        repo.close()

    print(f"\n--- SyncResult ---")
    print(f"  city                = {result.city}")
    print(f"  sync_time           = {result.sync_time.isoformat()}")
    print(f"  n_unsynced          = {result.n_unsynced}")
    print(f"  sheet_id            = {result.sheet_id}")
    print(f"  sheet_name          = {result.sheet_name}")
    print(f"  inserted_in_sheet   = {result.n_inserted_in_sheet}")
    print(f"  updated_in_sheet    = {result.n_updated_in_sheet}")
    print(f"  artifacts_uploaded  = {result.n_artifacts_uploaded}")
    print(f"  artifacts_skipped   = {result.n_artifacts_skipped}")
    print(f"  errors              = {len(result.errors)}")
    for e in result.errors:
        print(f"    - {e}")

    return 0 if not result.errors else 1


if __name__ == "__main__":
    sys.exit(main())
