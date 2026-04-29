"""Reverse-sync controller — pulls Drive state into a fresh local DB.

Direction: Drive → DB. Used when standing up Zelda on a new machine
so we don't have to re-discover (and re-pay Places API for) leads
that already exist on Drive.

Source of truth: the JSONL artifacts under
`raw-artifacts/{slug(city)}/`. Each line is a full Place Details API
response — lossless, unlike the sheet (which is a human-readable
subset). We never read sheet content here; the sheet is a projection,
not a source of truth.

Algorithm
---------
1. List `raw-artifacts/{slug(city)}/*.jsonl` on Drive.
2. Download anything we don't already have locally to
   `data/raw-artifacts/{slug(city)}/`.
3. Process every local JSONL chronologically (filenames sort that
   way: `YYYYMMDD-HHMMSS-XXXX`). For each line:
   - Parse the raw API response.
   - Build a RawLead via the existing converter, with `now` set to
     the file's run-id timestamp (so `discovered_at` reflects the
     original discovery time, not bootstrap time).
   - Upsert into the repo. The repo's UPSERT preserves
     `discovered_at` from the first sighting and bumps
     `last_modified_at` to the latest, so re-discovery snapshots are
     handled correctly when they exist.
4. Mark all touched leads as synced with `synced_at = bootstrap NOW`.
   Result: rows are immediately considered "in sync" with Drive
   (`last_modified_at <= last_synced_at`), so a follow-up sync is a
   no-op. If the user later re-discovers a lead, the discover flow
   bumps `last_modified_at`, the delta-detection contract puts it
   back into "unsynced", and the next sync push re-aligns Drive.

Idempotency
-----------
Running bootstrap twice on the same machine is safe and cheap: local
JSONLs are skipped (no re-download), repo upserts are no-ops if
nothing changed.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from loguru import logger

from zelda.controllers.sync import ARTIFACTS_DRIVE_FOLDER
from zelda.models.place import raw_lead_from_place_details
from zelda.models.raw_lead import RawLead
from zelda.repositories.raw_lead_repo import RawLeadRepository
from zelda.util import slugify


class _DriveGateway(Protocol):
    """The slice of GoogleDriveGateway that bootstrap needs."""

    def find_or_create_subfolder(
        self, name: str, *, parent_folder_id: str | None = None
    ) -> str: ...

    def list_files_in_folder(
        self, folder_id: str, *, mime_type: str | None = None
    ) -> list[dict]: ...

    def download_file(
        self, file_id: str, local_path: Path | str
    ) -> Path: ...


@dataclass
class BootstrapResult:
    city: str
    bootstrap_time: datetime
    n_drive_artifacts: int = 0       # how many JSONLs Drive listed
    n_files_downloaded: int = 0      # actually pulled (not skip-already-local)
    n_files_skipped_local: int = 0   # already on disk, didn't re-download
    n_files_processed: int = 0       # parsed for upsert
    n_lines_total: int = 0           # JSONL records seen
    n_lines_failed: int = 0          # skipped due to malformed JSON or convert error
    n_leads_upserted: int = 0        # unique place_ids upserted
    errors: list[str] = field(default_factory=list)


class BootstrapController:
    def __init__(
        self,
        drive: _DriveGateway,
        repo: RawLeadRepository,
        artifacts_dir: Path | str,
    ) -> None:
        self._drive = drive
        self._repo = repo
        self._artifacts_dir = Path(artifacts_dir)

    def bootstrap_city(self, city: str) -> BootstrapResult:
        if not city or not city.strip():
            raise ValueError("city must be non-empty")

        bootstrap_time = datetime.now(timezone.utc)
        result = BootstrapResult(city=city, bootstrap_time=bootstrap_time)

        logger.info("bootstrap.start city={city}", city=city)

        # 1. Locate the per-city Drive folder. If it doesn't exist on
        # Drive — nothing to bootstrap; not an error.
        artifacts_root = self._drive.find_or_create_subfolder(ARTIFACTS_DRIVE_FOLDER)
        city_folder_id = self._drive.find_or_create_subfolder(
            slugify(city), parent_folder_id=artifacts_root
        )
        drive_files = self._drive.list_files_in_folder(city_folder_id)
        # Defend against folders that hold non-JSONL stuff
        drive_files = [f for f in drive_files if f.get("name", "").endswith(".jsonl")]
        result.n_drive_artifacts = len(drive_files)

        if not drive_files:
            logger.info("bootstrap.empty city={city} — nothing on Drive", city=city)
            return result

        # 2. Download files we don't have locally yet.
        local_dir = self._artifacts_dir / slugify(city)
        local_dir.mkdir(parents=True, exist_ok=True)
        local_names = {p.name for p in local_dir.glob("*.jsonl")}

        for f in drive_files:
            name = f["name"]
            if name in local_names:
                result.n_files_skipped_local += 1
                continue
            try:
                self._drive.download_file(f["id"], local_dir / name)
                result.n_files_downloaded += 1
            except Exception as e:
                msg = f"download failed for {name}: {e}"
                logger.error(msg)
                result.errors.append(msg)

        # 3. Process every local JSONL chronologically. Filenames sort
        # by timestamp, so sorted() is the right order.
        local_files = sorted(local_dir.glob("*.jsonl"))
        upserted_place_ids: set[str] = set()

        for path in local_files:
            run_ts = _parse_run_id_timestamp(path.name) or bootstrap_time
            leads_in_file: list[RawLead] = []

            with path.open(encoding="utf-8") as fp:
                for line_num, line in enumerate(fp, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    result.n_lines_total += 1
                    try:
                        raw = json.loads(line)
                        lead = raw_lead_from_place_details(raw, city=city, now=run_ts)
                    except Exception as e:
                        msg = f"parse failed in {path.name}:{line_num}: {e}"
                        logger.error(msg)
                        result.errors.append(msg)
                        result.n_lines_failed += 1
                        continue
                    leads_in_file.append(lead)
                    upserted_place_ids.add(lead.place_id)

            if leads_in_file:
                self._repo.upsert_many(leads_in_file)
            result.n_files_processed += 1

            logger.info(
                "bootstrap.file file={name} run_ts={run_ts} leads_in_file={n}",
                name=path.name,
                run_ts=run_ts.isoformat(),
                n=len(leads_in_file),
            )

        result.n_leads_upserted = len(upserted_place_ids)

        # 4. Mark all touched leads as synced — they came from Drive,
        # so they're considered in-sync. Use bootstrap_time for the
        # synced_at stamp.
        if upserted_place_ids:
            self._repo.mark_synced(
                list(upserted_place_ids), synced_at=bootstrap_time
            )

        logger.info(
            "bootstrap.done city={city} drive_files={d} downloaded={dl} "
            "skipped={sk} processed={p} leads={l} errors={e}",
            city=city,
            d=result.n_drive_artifacts,
            dl=result.n_files_downloaded,
            sk=result.n_files_skipped_local,
            p=result.n_files_processed,
            l=result.n_leads_upserted,
            e=len(result.errors),
        )
        return result


# ── module-private helpers ──────────────────────────────────────────


def _parse_run_id_timestamp(filename: str) -> datetime | None:
    """Pull the run-id timestamp out of a filename like
    `20260429-125652-ad4e.jsonl`. Returns None if the filename doesn't
    follow the expected pattern."""
    stem = Path(filename).stem
    parts = stem.split("-")
    if len(parts) < 2:
        return None
    try:
        return datetime.strptime(
            f"{parts[0]} {parts[1]}", "%Y%m%d %H%M%S"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
