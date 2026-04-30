"""Concrete `SyncStep` implementations.

One class per lead source. Each step:
1. Pulls unsynced rows from its repo (delta-detection contract).
2. Navigates Drive: `{root}/{City}/discovery/{source}` (Sheet).
3. Projects each row onto a lossless sheet-friendly dict — JSON-
   typed model fields (lists, dicts) are passed through as-is; the
   `GoogleDriveGateway` serializes them to JSON cells.
4. Upserts by the source's natural key (place_id / profile_url).
5. Stamps `last_synced_at` on the persisted rows ONLY AFTER the
   sheet write succeeds (at-least-once delivery: a Drive failure
   leaves rows pending so the next tick retries).

Adding a new source's sync step is ~80 lines: define the constants,
the projection dict, and follow the same `run_for_city` skeleton.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from loguru import logger

from zelda.controllers.sync_pipeline import SyncStepResult
from zelda.models.google_places_lead import GooglePlacesLead
from zelda.models.lead_enrichment import LeadEnrichment
from zelda.models.lybrate_listing import LybrateListing
from zelda.models.practo_listing import PractoListing
from zelda.repositories.google_places_lead_repo import GooglePlacesLeadRepository
from zelda.repositories.lead_enrichment_repo import LeadEnrichmentRepository
from zelda.repositories.lybrate_listing_repo import LybrateListingRepository
from zelda.repositories.practo_listing_repo import PractoListingRepository
from zelda.util import slugify


# Drive folder hierarchy:
#   {root}/{City}/discovery/{source}                     ← Sheet
#   {root}/{City}/discovery/raw-artifacts/{*.jsonl}      ← Google Places only
#   {root}/{City}/enrichment/leads                       ← Enrichment sheet
DISCOVERY_FOLDER_NAME = "discovery"
ARTIFACTS_FOLDER_NAME = "raw-artifacts"
ENRICHMENT_FOLDER_NAME = "enrichment"


class _DriveGateway(Protocol):
    """Structural type — the subset of `GoogleDriveGateway` we depend on."""

    def find_or_create_subfolder(
        self, name: str, *, parent_folder_id: str | None = None,
    ) -> str: ...

    def find_or_create_spreadsheet(
        self, name: str, *, parent_folder_id: str | None = None,
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
        self, folder_id: str, *, mime_type: str | None = None,
    ) -> list[dict]: ...


def _navigate_to_discovery_folder(
    drive: _DriveGateway, city: str,
) -> str:
    """Find/create `{root}/{City}/discovery/`. Returns the discovery
    folder ID. Idempotent — both calls upsert."""
    city_folder = drive.find_or_create_subfolder(city)
    return drive.find_or_create_subfolder(
        DISCOVERY_FOLDER_NAME, parent_folder_id=city_folder,
    )


# ── GooglePlacesSyncStep ────────────────────────────────────────────


# Lossless header: every column in `google_places_leads`. JSON-typed
# columns (`reviews`, `types`, `address_components`, `opening_hours`,
# `extras`, `raw_json`) are passed through as Python dicts/lists and
# the gateway JSON-stringifies them at write time.
GOOGLE_PLACES_HEADER: list[str] = [
    "place_id",
    "city",
    "name",
    "formatted_address",
    "short_address",
    "address_components",
    "lat",
    "lng",
    "phone",
    "phone_intl",
    "website",
    "google_maps_url",
    "rating",
    "review_count",
    "reviews",
    "business_status",
    "primary_type",
    "types",
    "price_level",
    "editorial_summary",
    "photos_count",
    "opening_hours",
    "extras",
    "raw_json",
    "discovered_at",
    "last_modified_at",
    "last_synced_at",
]


class GooglePlacesSyncStep:
    """Sync `google_places_leads` to a Drive sheet.

    Plus: mirror local JSONL artifacts from
    `{artifacts_dir}/{slug(city)}/*.jsonl` into Drive at
    `{root}/{City}/discovery/raw-artifacts/`. Diff-based — files
    already on Drive (by name) are skipped. Artifact mirroring is
    only relevant for Google Places (the only source that writes
    JSONL).
    """

    name = "google_places"

    def __init__(
        self,
        drive: _DriveGateway,
        repo: GooglePlacesLeadRepository,
        artifacts_dir: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._drive = drive
        self._repo = repo
        self._artifacts_dir = Path(artifacts_dir)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def run_for_city(self, city: str, *, run_id: str) -> SyncStepResult:
        started_at = self._clock()
        result = SyncStepResult(
            step_name=self.name, city=city, started_at=started_at,
        )

        unsynced = self._repo.get_unsynced_for_city(city)
        result.pulled = len(unsynced)

        if unsynced:
            try:
                discovery_folder = _navigate_to_discovery_folder(self._drive, city)
                sheet_id = self._drive.find_or_create_spreadsheet(
                    self.name, parent_folder_id=discovery_folder,
                )
                rows = [
                    _google_places_lead_to_dict(lead, sync_time=started_at)
                    for lead in unsynced
                ]
                stats = self._drive.upsert_sheet_rows_by_key(
                    sheet_id,
                    header=GOOGLE_PLACES_HEADER,
                    rows=rows,
                    key_column="place_id",
                )
                self._repo.mark_synced(
                    [lead.place_id for lead in unsynced], synced_at=started_at,
                )
                result.inserted = stats["inserted"]
                result.updated = stats["updated"]
                result.extras["sheet_id"] = sheet_id
            except Exception as e:  # noqa: BLE001
                msg = f"sheet sync failed: {type(e).__name__}: {e}"
                logger.error(
                    "google_places_sync.sheet_error run_id={r} city={c} err={e}",
                    r=run_id, c=city, e=msg,
                )
                result.errors.append(msg)

        # Artifact mirror runs even when no rows were unsynced — a
        # JSONL file from a previous run might still need uploading.
        try:
            uploaded, skipped = self._sync_artifacts(city)
            result.extras["artifacts_uploaded"] = uploaded
            result.extras["artifacts_skipped"] = skipped
        except Exception as e:  # noqa: BLE001
            msg = f"artifact mirror failed: {type(e).__name__}: {e}"
            logger.error(
                "google_places_sync.artifact_error run_id={r} city={c} err={e}",
                r=run_id, c=city, e=msg,
            )
            result.errors.append(msg)

        result.finished_at = self._clock()
        return result

    def _sync_artifacts(self, city: str) -> tuple[int, int]:
        local_dir = self._artifacts_dir / slugify(city)
        if not local_dir.exists() or not local_dir.is_dir():
            return 0, 0
        local_files = sorted(local_dir.glob("*.jsonl"))
        if not local_files:
            return 0, 0

        discovery_folder = _navigate_to_discovery_folder(self._drive, city)
        artifacts_folder = self._drive.find_or_create_subfolder(
            ARTIFACTS_FOLDER_NAME, parent_folder_id=discovery_folder,
        )
        on_drive = {
            f["name"] for f in self._drive.list_files_in_folder(artifacts_folder)
        }

        uploaded = skipped = 0
        for f in local_files:
            if f.name in on_drive:
                skipped += 1
                continue
            self._drive.upload_file(
                f, parent_folder_id=artifacts_folder,
                mime_type="application/x-ndjson",
            )
            uploaded += 1
        return uploaded, skipped


def _google_places_lead_to_dict(
    lead: GooglePlacesLead, *, sync_time: datetime,
) -> dict[str, Any]:
    return {
        "place_id": lead.place_id,
        "city": lead.city,
        "name": lead.name,
        "formatted_address": lead.formatted_address,
        "short_address": lead.short_address,
        "address_components": lead.address_components,
        "lat": lead.lat,
        "lng": lead.lng,
        "phone": lead.phone,
        "phone_intl": lead.phone_intl,
        "website": lead.website,
        "google_maps_url": lead.google_maps_url,
        "rating": lead.rating,
        "review_count": lead.review_count,
        "reviews": lead.reviews,
        "business_status": lead.business_status,
        "primary_type": lead.primary_type,
        "types": lead.types,
        "price_level": lead.price_level,
        "editorial_summary": lead.editorial_summary,
        "photos_count": lead.photos_count,
        "opening_hours": lead.opening_hours,
        "extras": lead.extras,
        "raw_json": lead.raw_json,
        "discovered_at": lead.discovered_at,
        "last_modified_at": lead.last_modified_at,
        "last_synced_at": sync_time,
    }


# ── PractoSyncStep ──────────────────────────────────────────────────


PRACTO_HEADER: list[str] = [
    "profile_url",
    "city",
    "name",
    "address",
    "lat",
    "lng",
    "raw_json",
    "discovered_at",
    "last_modified_at",
    "last_synced_at",
]


class PractoSyncStep:
    """Sync `practo_listings` to a Drive sheet at
    `{root}/{City}/discovery/practo`."""

    name = "practo"

    def __init__(
        self,
        drive: _DriveGateway,
        repo: PractoListingRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._drive = drive
        self._repo = repo
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def run_for_city(self, city: str, *, run_id: str) -> SyncStepResult:
        started_at = self._clock()
        result = SyncStepResult(
            step_name=self.name, city=city, started_at=started_at,
        )

        unsynced = self._repo.get_unsynced_for_city(city)
        result.pulled = len(unsynced)

        if not unsynced:
            result.finished_at = self._clock()
            return result

        try:
            discovery_folder = _navigate_to_discovery_folder(self._drive, city)
            sheet_id = self._drive.find_or_create_spreadsheet(
                self.name, parent_folder_id=discovery_folder,
            )
            rows = [
                _practo_listing_to_dict(l, sync_time=started_at)
                for l in unsynced
            ]
            stats = self._drive.upsert_sheet_rows_by_key(
                sheet_id,
                header=PRACTO_HEADER,
                rows=rows,
                key_column="profile_url",
            )
            self._repo.mark_synced(
                [l.profile_url for l in unsynced], synced_at=started_at,
            )
            result.inserted = stats["inserted"]
            result.updated = stats["updated"]
            result.extras["sheet_id"] = sheet_id
        except Exception as e:  # noqa: BLE001
            msg = f"sheet sync failed: {type(e).__name__}: {e}"
            logger.error(
                "practo_sync.sheet_error run_id={r} city={c} err={e}",
                r=run_id, c=city, e=msg,
            )
            result.errors.append(msg)

        result.finished_at = self._clock()
        return result


def _practo_listing_to_dict(
    l: PractoListing, *, sync_time: datetime,
) -> dict[str, Any]:
    return {
        "profile_url": l.profile_url,
        "city": l.city,
        "name": l.name,
        "address": l.address,
        "lat": l.lat,
        "lng": l.lng,
        "raw_json": l.raw_json,
        "discovered_at": l.discovered_at,
        "last_modified_at": l.last_modified_at,
        "last_synced_at": sync_time,
    }


# ── LybrateSyncStep ─────────────────────────────────────────────────


LYBRATE_HEADER: list[str] = [
    "profile_url",
    "city",
    "doctor_name",
    "clinic_name",
    "address",
    "locality",
    "postal_code",
    "lat",
    "lng",
    "phone",
    "specialty",
    "raw_json",
    "discovered_at",
    "last_modified_at",
    "last_synced_at",
]


class LybrateSyncStep:
    """Sync `lybrate_listings` to a Drive sheet at
    `{root}/{City}/discovery/lybrate`."""

    name = "lybrate"

    def __init__(
        self,
        drive: _DriveGateway,
        repo: LybrateListingRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._drive = drive
        self._repo = repo
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def run_for_city(self, city: str, *, run_id: str) -> SyncStepResult:
        started_at = self._clock()
        result = SyncStepResult(
            step_name=self.name, city=city, started_at=started_at,
        )

        unsynced = self._repo.get_unsynced_for_city(city)
        result.pulled = len(unsynced)

        if not unsynced:
            result.finished_at = self._clock()
            return result

        try:
            discovery_folder = _navigate_to_discovery_folder(self._drive, city)
            sheet_id = self._drive.find_or_create_spreadsheet(
                self.name, parent_folder_id=discovery_folder,
            )
            rows = [
                _lybrate_listing_to_dict(l, sync_time=started_at)
                for l in unsynced
            ]
            stats = self._drive.upsert_sheet_rows_by_key(
                sheet_id,
                header=LYBRATE_HEADER,
                rows=rows,
                key_column="profile_url",
            )
            self._repo.mark_synced(
                [l.profile_url for l in unsynced], synced_at=started_at,
            )
            result.inserted = stats["inserted"]
            result.updated = stats["updated"]
            result.extras["sheet_id"] = sheet_id
        except Exception as e:  # noqa: BLE001
            msg = f"sheet sync failed: {type(e).__name__}: {e}"
            logger.error(
                "lybrate_sync.sheet_error run_id={r} city={c} err={e}",
                r=run_id, c=city, e=msg,
            )
            result.errors.append(msg)

        result.finished_at = self._clock()
        return result


def _lybrate_listing_to_dict(
    l: LybrateListing, *, sync_time: datetime,
) -> dict[str, Any]:
    return {
        "profile_url": l.profile_url,
        "city": l.city,
        "doctor_name": l.doctor_name,
        "clinic_name": l.clinic_name,
        "address": l.address,
        "locality": l.locality,
        "postal_code": l.postal_code,
        "lat": l.lat,
        "lng": l.lng,
        "phone": l.phone,
        "specialty": l.specialty,
        "raw_json": l.raw_json,
        "discovered_at": l.discovered_at,
        "last_modified_at": l.last_modified_at,
        "last_synced_at": sync_time,
    }


# ── EnrichmentSyncStep ──────────────────────────────────────────────


ENRICHMENT_HEADER: list[str] = [
    # Identity
    "lead_id", "name", "city",
    # Scoring — most important for triage
    "need_score", "score_tier", "pitch_angle",
    # Contact — for outreach
    "owner_name", "owner_qualifications", "direct_phone",
    # Reputation
    "google_review_count", "google_rating",
    "review_velocity_30d", "review_velocity_90d",
    "owner_response_rate", "owner_avg_response_days",
    "has_revenue_leak_signal", "negative_theme_flags",
    # Acquisition / online presence
    "has_website", "website_loads", "website_is_mobile_friendly",
    "website_has_schema_markup", "website_has_blog", "website_agency_credit",
    "on_practo", "on_lybrate", "source_count",
    "nap_consistent", "is_chain", "is_hospital_embedded", "is_not_operational",
    # Conversion
    "has_whatsapp_link", "has_online_booking", "has_chat_widget",
    "practo_booking_enabled",
    # Practice details (Practo / website)
    "practo_review_count", "practo_rating", "practo_consultation_fee_inr",
    "years_in_operation", "dentist_count", "service_mix", "equipment_claims",
    # GBP completeness
    "gbp_has_hours", "gbp_photos_count", "gbp_has_description",
    # Metadata
    "passes_completed", "updated_at",
]


class EnrichmentSyncStep:
    """Sync `lead_enrichments` to Drive at `{root}/{City}/enrichment/leads`.

    Always does a full sync (no delta tracking). The enrichment table
    is small (~hundreds of leads) and changes frequently as passes run.
    Rows are ordered by need_score DESC so the hottest leads appear at
    the top of the sheet.

    Fully self-contained — reads only from `enrichment_repo`. The clinic
    name is stored in `LeadEnrichment.clinic_name` (set by Pass 0) so no
    cross-table join is needed.
    """

    name = "enrichment"

    def __init__(
        self,
        drive: _DriveGateway,
        enrichment_repo: LeadEnrichmentRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._drive = drive
        self._enrichment_repo = enrichment_repo
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def run_for_city(self, city: str, *, run_id: str) -> SyncStepResult:
        started_at = self._clock()
        result = SyncStepResult(step_name=self.name, city=city, started_at=started_at)

        enrichments = self._enrichment_repo.get_for_city(city)
        result.pulled = len(enrichments)

        if not enrichments:
            result.finished_at = self._clock()
            return result

        try:
            city_folder = self._drive.find_or_create_subfolder(city)
            enrichment_folder = self._drive.find_or_create_subfolder(
                ENRICHMENT_FOLDER_NAME, parent_folder_id=city_folder,
            )
            sheet_id = self._drive.find_or_create_spreadsheet(
                "leads", parent_folder_id=enrichment_folder,
            )
            rows = [_enrichment_to_dict(e) for e in enrichments]
            stats = self._drive.upsert_sheet_rows_by_key(
                sheet_id,
                header=ENRICHMENT_HEADER,
                rows=rows,
                key_column="lead_id",
            )
            result.inserted = stats["inserted"]
            result.updated = stats["updated"]
            result.extras["sheet_id"] = sheet_id
        except Exception as e:  # noqa: BLE001
            msg = f"sheet sync failed: {type(e).__name__}: {e}"
            logger.error(
                "enrichment_sync.sheet_error run_id={r} city={c} err={e}",
                r=run_id, c=city, e=msg,
            )
            result.errors.append(msg)

        result.finished_at = self._clock()
        return result


def _enrichment_to_dict(e: LeadEnrichment) -> dict[str, Any]:
    return {
        "lead_id": e.lead_id,
        "name": e.clinic_name,
        "city": e.city,
        "need_score": e.need_score,
        "score_tier": e.score_tier,
        "pitch_angle": e.pitch_angle,
        "owner_name": e.owner_name,
        "owner_qualifications": e.owner_qualifications,
        "direct_phone": e.direct_phone,
        "google_review_count": e.google_review_count,
        "google_rating": e.google_rating,
        "review_velocity_30d": e.review_velocity_30d,
        "review_velocity_90d": e.review_velocity_90d,
        "owner_response_rate": e.owner_response_rate,
        "owner_avg_response_days": e.owner_avg_response_days,
        "has_revenue_leak_signal": e.has_revenue_leak_signal,
        "negative_theme_flags": e.negative_theme_flags,
        "has_website": e.has_website,
        "website_loads": e.website_loads,
        "website_is_mobile_friendly": e.website_is_mobile_friendly,
        "website_has_schema_markup": e.website_has_schema_markup,
        "website_has_blog": e.website_has_blog,
        "website_agency_credit": e.website_agency_credit,
        "on_practo": e.on_practo,
        "on_lybrate": e.on_lybrate,
        "source_count": e.source_count,
        "nap_consistent": e.nap_consistent,
        "is_chain": e.is_chain,
        "is_hospital_embedded": e.is_hospital_embedded,
        "is_not_operational": e.is_not_operational,
        "has_whatsapp_link": e.has_whatsapp_link,
        "has_online_booking": e.has_online_booking,
        "has_chat_widget": e.has_chat_widget,
        "practo_booking_enabled": e.practo_booking_enabled,
        "practo_review_count": e.practo_review_count,
        "practo_rating": e.practo_rating,
        "practo_consultation_fee_inr": e.practo_consultation_fee_inr,
        "years_in_operation": e.years_in_operation,
        "dentist_count": e.dentist_count,
        "service_mix": e.service_mix,
        "equipment_claims": e.equipment_claims,
        "gbp_has_hours": e.gbp_has_hours,
        "gbp_photos_count": e.gbp_photos_count,
        "gbp_has_description": e.gbp_has_description,
        "passes_completed": e.passes_completed,
        "updated_at": e.updated_at,
    }


__all__ = [
    "DISCOVERY_FOLDER_NAME",
    "ARTIFACTS_FOLDER_NAME",
    "ENRICHMENT_FOLDER_NAME",
    "ENRICHMENT_HEADER",
    "EnrichmentSyncStep",
    "GOOGLE_PLACES_HEADER",
    "GooglePlacesSyncStep",
    "LYBRATE_HEADER",
    "LybrateSyncStep",
    "PRACTO_HEADER",
    "PractoSyncStep",
]
