"""`PractoDirectoryController` — orchestrates Practo discovery for one
city: fetch the directory, dedup against existing rows, persist.

Discovery layering
------------------
- Gateway (`PractoDirectoryGateway`) returns ephemeral
  `PractoDirectoryEntry` records straight from Practo's HTML.
- Controller (this module) attaches discovery-time housekeeping
  (city, discovered_at, last_modified_at), filters down to genuinely
  new entries, and writes via `PractoListingRepository`.
- Step (`PractoDiscoveryStep`) is a thin shim that adapts the
  controller's result to the pipeline's `StepResult` shape.

Idempotency
-----------
Re-running for a city is safe. Practo's directory is small (<100
entries per Indian city); we always re-fetch the full directory and
let the repo's upsert handle merge semantics. A re-run with no
upstream changes leaves `last_modified_at` bumped on every row but
does not produce a sync delta because the column values are
identical to what's on disk — the repo's UPSERT…DO UPDATE writes
back the same values.

Note: Practo's directory crawl is essentially free (1 RTT × ~5–10
pages), so we don't expose a "max entries" cost knob like Google
Places.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol

from loguru import logger

from zelda.models.practo_listing import PractoListing
from zelda.repositories.practo_listing_repo import PractoListingRepository


class _DirectoryGatewayProtocol(Protocol):
    """Structural type — what the controller needs from the gateway."""

    def fetch_for_city(self, city: str) -> list: ...


@dataclass
class PractoDirectoryResult:
    """One run's outcome. Used by `PractoDiscoveryStep` to populate
    the pipeline's uniform `StepResult`."""

    city: str
    run_id: str
    started_at: datetime
    finished_at: datetime | None = None

    discovered: int = 0
    """Entries returned by the gateway (after intra-run dedup)."""
    inserted: int = 0
    """New rows written. Equals `len(discovered) - len(already_known)`."""
    already_known: int = 0
    """Entries whose `profile_url` was already in the listings table."""
    errors: list[str] = field(default_factory=list)


class PractoDirectoryController:
    """Fetch Practo's directory for one city and persist new entries."""

    def __init__(
        self,
        gateway: _DirectoryGatewayProtocol,
        repo: PractoListingRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._gateway = gateway
        self._repo = repo
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def run(self, city: str, *, run_id: str) -> PractoDirectoryResult:
        if not city or not city.strip():
            raise ValueError("city must be non-empty")

        result = PractoDirectoryResult(
            city=city, run_id=run_id, started_at=self._clock(),
        )

        logger.info(
            "practo_directory.start run_id={r} city={c}",
            r=run_id, c=city,
        )

        try:
            entries = self._gateway.fetch_for_city(city)
        except Exception as e:  # noqa: BLE001
            msg = f"gateway fetch_for_city failed: {type(e).__name__}: {e}"
            logger.error(
                "practo_directory.gateway_error run_id={r} city={c} err={e}",
                r=run_id, c=city, e=msg,
            )
            result.errors.append(msg)
            result.finished_at = self._clock()
            return result

        result.discovered = len(entries)

        # Bulk existence check — one SQL round trip instead of N.
        urls = [e.profile_url for e in entries]
        already_known = self._repo.exists_many(urls) if urls else set()
        result.already_known = len(already_known)

        # Build PractoListing rows for ALL discovered entries (so re-
        # runs touch last_modified_at consistently). The repo's UPSERT
        # behavior preserves discovered_at on existing rows.
        ts = self._clock()
        listings = [
            PractoListing(
                profile_url=e.profile_url,
                city=city,
                name=e.name,
                address=e.address or None,
                lat=e.lat,
                lng=e.lng,
                discovered_at=ts,
                last_modified_at=ts,
                raw_json={
                    "name": e.name,
                    "address": e.address,
                    "lat": e.lat,
                    "lng": e.lng,
                },
            )
            for e in entries
        ]

        try:
            self._repo.upsert_many(listings)
        except Exception as e:  # noqa: BLE001
            msg = f"repo upsert failed: {type(e).__name__}: {e}"
            logger.error(
                "practo_directory.upsert_error run_id={r} city={c} err={e}",
                r=run_id, c=city, e=msg,
            )
            result.errors.append(msg)
            result.finished_at = self._clock()
            return result

        result.inserted = max(0, len(listings) - result.already_known)
        result.finished_at = self._clock()

        logger.info(
            "practo_directory.done run_id={r} city={c} discovered={d} "
            "inserted={i} already_known={ak} errors={e}",
            r=run_id, c=city, d=result.discovered, i=result.inserted,
            ak=result.already_known, e=len(result.errors),
        )
        return result


__all__ = [
    "PractoDirectoryController",
    "PractoDirectoryResult",
]
