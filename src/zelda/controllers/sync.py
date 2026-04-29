"""Drive sync controller — projects RawLead state from SQLite onto a
Google Sheet, and mirrors local JSONL artifacts into Drive.

Direction: DB → Drive. Always. (Reverse sync — pulling Drive state
into a fresh local DB on a new machine — is phase 9.)

Trigger model
-------------
This controller is callable directly (one-shot CLI) or by an OS
scheduler (cron, launchd) for periodic sync. It is idempotent on
re-run thanks to the delta-detection contract enforced by
`RawLeadRepository.get_unsynced_for_city`:
    last_synced_at IS NULL OR last_modified_at > last_synced_at

Decoupling
----------
The discovery flow does NOT call this controller. Discovery only
touches SQLite. Sync is a separate concern that observes the DB and
propagates changes to Drive. They run on independent cadences.

What gets synced
----------------
1. Per-city Google Sheet "Zelda — Raw Leads — Dentists — {City}" with
   the columns in `RAW_LEAD_SHEET_HEADER` (a human-readable subset
   of the RawLead model — `raw_json`, `reviews`, `extras`, etc. stay
   in the DB and the JSONL artifact, not in the sheet).
2. Per-city JSONL artifact dumps mirrored into a `raw-artifacts/{city}/`
   subfolder. Diff-based: artifacts already on Drive are skipped.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from loguru import logger

from zelda.models.raw_lead import RawLead
from zelda.repositories.raw_lead_repo import RawLeadRepository
from zelda.util import slugify


SHEET_NAME_PREFIX = "Zelda — Raw Leads — Dentists"

ARTIFACTS_DRIVE_FOLDER = "raw-artifacts"

# Order matters — this is the column order in the sheet.
RAW_LEAD_SHEET_HEADER: list[str] = [
    "place_id",
    "name",
    "rating",
    "review_count",
    "business_status",
    "primary_type",
    "phone",
    "phone_intl",
    "website",
    "google_maps_url",
    "formatted_address",
    "short_address",
    "lat",
    "lng",
    "types",
    "price_level",
    "editorial_summary",
    "photos_count",
    "discovered_at",
    "last_modified_at",
    "last_synced_at",
]


def sheet_name_for_city(city: str) -> str:
    return f"{SHEET_NAME_PREFIX} — {city}"


class _DriveGateway(Protocol):
    """Structural type — what this controller needs from the gateway."""

    def find_or_create_subfolder(
        self, name: str, *, parent_folder_id: str | None = None
    ) -> str: ...

    def find_or_create_spreadsheet(
        self, name: str, *, parent_folder_id: str | None = None
    ) -> str: ...

    def upsert_sheet_rows_by_key(
        self,
        spreadsheet_id: str,
        *,
        header: list[str],
        rows: list[dict],
        key_column: str,
    ) -> dict[str, int]: ...

    def upload_file(
        self,
        local_path: Path | str,
        *,
        parent_folder_id: str | None = None,
        drive_name: str | None = None,
        mime_type: str = "application/octet-stream",
    ) -> str: ...

    def list_files_in_folder(
        self, folder_id: str, *, mime_type: str | None = None
    ) -> list[dict]: ...


@dataclass
class SyncResult:
    city: str
    sync_time: datetime
    n_unsynced: int = 0
    sheet_id: str | None = None
    sheet_name: str | None = None
    n_inserted_in_sheet: int = 0
    n_updated_in_sheet: int = 0
    n_artifacts_uploaded: int = 0
    n_artifacts_skipped: int = 0
    errors: list[str] = field(default_factory=list)


class DriveSyncController:
    def __init__(
        self,
        drive: _DriveGateway,
        repo: RawLeadRepository,
        artifacts_dir: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._drive = drive
        self._repo = repo
        self._artifacts_dir = Path(artifacts_dir)
        # Injectable clock so tests can pin sync_time deterministically.
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def sync_city(self, city: str) -> SyncResult:
        if not city or not city.strip():
            raise ValueError("city must be non-empty")

        sync_time = self._clock()
        result = SyncResult(city=city, sync_time=sync_time)

        logger.info("sync.start city={city}", city=city)

        # 1. Sheet sync (delta-detection + upsert).
        unsynced = self._repo.get_unsynced_for_city(city)
        result.n_unsynced = len(unsynced)

        if unsynced:
            try:
                self._sync_sheet(city, unsynced, sync_time, result)
                # Only mark synced after the sheet write succeeds — if Drive
                # fails halfway, the rows stay "unsynced" so the next run
                # re-tries. This is the at-least-once delivery property.
                self._repo.mark_synced(
                    [lead.place_id for lead in unsynced], synced_at=sync_time
                )
                logger.info(
                    "sync.sheet city={city} sheet={sheet!r} "
                    "inserted={i} updated={u}",
                    city=city,
                    sheet=result.sheet_name,
                    i=result.n_inserted_in_sheet,
                    u=result.n_updated_in_sheet,
                )
            except Exception as e:
                msg = f"sheet sync failed: {e}"
                logger.error(msg)
                result.errors.append(msg)

        # 2. Artifact sync (always, even when no rows to push — the user
        # could have an artifact from a prior run that didn't make it up).
        try:
            uploaded, skipped = self._sync_artifacts(city)
            result.n_artifacts_uploaded = uploaded
            result.n_artifacts_skipped = skipped
        except Exception as e:
            msg = f"artifact sync failed: {e}"
            logger.error(msg)
            result.errors.append(msg)

        logger.info(
            "sync.done city={city} unsynced={n} inserted={i} updated={u} "
            "artifacts_uploaded={au} artifacts_skipped={asn} errors={e}",
            city=city,
            n=result.n_unsynced,
            i=result.n_inserted_in_sheet,
            u=result.n_updated_in_sheet,
            au=result.n_artifacts_uploaded,
            asn=result.n_artifacts_skipped,
            e=len(result.errors),
        )
        return result

    # ── pipeline stages ──────────────────────────────────────────────

    def _sync_sheet(
        self,
        city: str,
        leads: list[RawLead],
        sync_time: datetime,
        result: SyncResult,
    ) -> None:
        sheet_name = sheet_name_for_city(city)
        result.sheet_name = sheet_name
        sheet_id = self._drive.find_or_create_spreadsheet(sheet_name)
        result.sheet_id = sheet_id

        rows = [_lead_to_sheet_row(lead, sync_time=sync_time) for lead in leads]
        upsert_stats = self._drive.upsert_sheet_rows_by_key(
            sheet_id,
            header=RAW_LEAD_SHEET_HEADER,
            rows=rows,
            key_column="place_id",
        )
        result.n_inserted_in_sheet = upsert_stats["inserted"]
        result.n_updated_in_sheet = upsert_stats["updated"]

    def _sync_artifacts(self, city: str) -> tuple[int, int]:
        """Mirror local JSONL artifacts to Drive. Diff-based: skip files
        that already exist on Drive by name. Returns (uploaded, skipped)."""
        local_dir = self._artifacts_dir / slugify(city)
        if not local_dir.exists() or not local_dir.is_dir():
            return 0, 0

        local_files = sorted(local_dir.glob("*.jsonl"))
        if not local_files:
            return 0, 0

        artifacts_root = self._drive.find_or_create_subfolder(
            ARTIFACTS_DRIVE_FOLDER
        )
        city_folder_id = self._drive.find_or_create_subfolder(
            slugify(city), parent_folder_id=artifacts_root
        )
        on_drive_names = {
            f["name"] for f in self._drive.list_files_in_folder(city_folder_id)
        }

        uploaded = 0
        skipped = 0
        for f in local_files:
            if f.name in on_drive_names:
                skipped += 1
                continue
            self._drive.upload_file(
                f,
                parent_folder_id=city_folder_id,
                mime_type="application/x-ndjson",
            )
            uploaded += 1
        return uploaded, skipped


# ── module-private helpers ──────────────────────────────────────────


def _lead_to_sheet_row(lead: RawLead, *, sync_time: datetime) -> dict:
    """Project a RawLead onto a sheet-friendly dict.

    `last_synced_at` is set to the current sync_time (not the lead's
    stored value) so what's in the sheet matches what we're about to
    write to the DB on `mark_synced`.
    """
    return {
        "place_id": lead.place_id,
        "name": lead.name,
        "rating": lead.rating,
        "review_count": lead.review_count,
        "business_status": lead.business_status,
        "primary_type": lead.primary_type,
        "phone": lead.phone,
        "phone_intl": lead.phone_intl,
        "website": lead.website,
        "google_maps_url": lead.google_maps_url,
        "formatted_address": lead.formatted_address,
        "short_address": lead.short_address,
        "lat": lead.lat,
        "lng": lead.lng,
        "types": lead.types,
        "price_level": lead.price_level,
        "editorial_summary": lead.editorial_summary,
        "photos_count": lead.photos_count,
        "discovered_at": lead.discovered_at,
        "last_modified_at": lead.last_modified_at,
        "last_synced_at": sync_time,
    }
