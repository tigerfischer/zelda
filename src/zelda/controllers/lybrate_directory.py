"""`LybrateDirectoryController` — orchestrates Lybrate discovery for
one city: fetch the directory, dedup against existing rows, persist.

Same shape as `PractoDirectoryController`. The only differences are:
- Different gateway, different repo, different table.
- Lybrate entries are doctor-keyed (one row per doctor) whereas
  Practo's are clinic-keyed. The cross-source matching phase will
  reconcile clinics that have multiple Lybrate doctor entries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol

from loguru import logger

from zelda.models.lybrate_listing import LybrateListing
from zelda.repositories.lybrate_listing_repo import LybrateListingRepository


class _DirectoryGatewayProtocol(Protocol):
    def fetch_for_city(self, city: str) -> list: ...


@dataclass
class LybrateDirectoryResult:
    """One run's outcome. Used by `LybrateDiscoveryStep` to populate
    the pipeline's uniform `StepResult`."""

    city: str
    run_id: str
    started_at: datetime
    finished_at: datetime | None = None

    discovered: int = 0
    inserted: int = 0
    already_known: int = 0
    errors: list[str] = field(default_factory=list)


class LybrateDirectoryController:
    """Fetch Lybrate's directory for one city and persist new entries."""

    def __init__(
        self,
        gateway: _DirectoryGatewayProtocol,
        repo: LybrateListingRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._gateway = gateway
        self._repo = repo
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def run(self, city: str, *, run_id: str) -> LybrateDirectoryResult:
        if not city or not city.strip():
            raise ValueError("city must be non-empty")

        result = LybrateDirectoryResult(
            city=city, run_id=run_id, started_at=self._clock(),
        )

        logger.info(
            "lybrate_directory.start run_id={r} city={c}",
            r=run_id, c=city,
        )

        try:
            entries = self._gateway.fetch_for_city(city)
        except Exception as e:  # noqa: BLE001
            msg = f"gateway fetch_for_city failed: {type(e).__name__}: {e}"
            logger.error(
                "lybrate_directory.gateway_error run_id={r} city={c} err={e}",
                r=run_id, c=city, e=msg,
            )
            result.errors.append(msg)
            result.finished_at = self._clock()
            return result

        result.discovered = len(entries)

        urls = [e.profile_url for e in entries]
        already_known = self._repo.exists_many(urls) if urls else set()
        result.already_known = len(already_known)

        ts = self._clock()
        listings = [
            LybrateListing(
                profile_url=e.profile_url,
                city=city,
                doctor_name=e.doctor_name,
                clinic_name=None,        # not in listing JSON-LD
                address=e.address,
                locality=e.locality,
                postal_code=e.postal_code,
                lat=e.lat,
                lng=e.lng,
                phone=None,              # not in listing JSON-LD
                specialty=e.specialty,
                discovered_at=ts,
                last_modified_at=ts,
                raw_json={
                    "doctor_name": e.doctor_name,
                    "address": e.address,
                    "locality": e.locality,
                    "postal_code": e.postal_code,
                    "lat": e.lat,
                    "lng": e.lng,
                    "specialty": e.specialty,
                },
            )
            for e in entries
        ]

        try:
            self._repo.upsert_many(listings)
        except Exception as e:  # noqa: BLE001
            msg = f"repo upsert failed: {type(e).__name__}: {e}"
            logger.error(
                "lybrate_directory.upsert_error run_id={r} city={c} err={e}",
                r=run_id, c=city, e=msg,
            )
            result.errors.append(msg)
            result.finished_at = self._clock()
            return result

        result.inserted = max(0, len(listings) - result.already_known)
        result.finished_at = self._clock()

        logger.info(
            "lybrate_directory.done run_id={r} city={c} discovered={d} "
            "inserted={i} already_known={ak} errors={e}",
            r=run_id, c=city, d=result.discovered, i=result.inserted,
            ak=result.already_known, e=len(result.errors),
        )
        return result


__all__ = [
    "LybrateDirectoryController",
    "LybrateDirectoryResult",
]
