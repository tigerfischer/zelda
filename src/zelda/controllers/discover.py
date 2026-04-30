"""Discovery controller — orchestrates the Places gateway + repo to
populate raw leads for a city.

The pipeline:
    text_search × N queries  →  dedupe  →  skip already-known place_ids
    →  cap to max_results    →  fetch place details  →  GooglePlacesLead
    →  write JSONL artifact  →  upsert to repo

Cost knob:
    `max_results` caps the number of Place Details calls (the expensive
    call). Default in the controller is `None` (unlimited); the CLI
    defaults to a low integer for safety.

Re-run policy:
    place_ids that already exist in the repo are skipped entirely —
    not re-fetched, not re-stored, not re-mirrored to the JSONL
    artifact. This is the explicit user policy for V1.
"""

import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from loguru import logger

from zelda.models.place import Place, google_places_lead_from_place_details
from zelda.models.google_places_lead import GooglePlacesLead
from zelda.repositories.google_places_lead_repo import GooglePlacesLeadRepository
from zelda.util import slugify


DEFAULT_QUERIES: tuple[str, ...] = (
    "dentist in {city}",
    "dental clinic in {city}",
    "dental hospital in {city}",
    "orthodontist in {city}",
    "pediatric dentist in {city}",
    "dental implants in {city}",
    "cosmetic dentist in {city}",
)


class _PlacesGateway(Protocol):
    """Structural type. Lets tests pass a fake without importing the
    real httpx-backed gateway."""

    def text_search(self, query: str, *, max_pages: int = 1) -> list[Place]: ...

    def get_place_details(self, place_id: str) -> dict: ...


@dataclass
class DiscoverResult:
    """Summary stats from one discovery run. Useful for CLI output and
    for future monitoring."""

    run_id: str
    city: str
    text_search_total: int = 0       # raw text-search hits, may include dupes
    deduped_total: int = 0           # unique place_ids across queries
    already_known_count: int = 0     # skipped because in DB (re-run policy)
    new_eligible_count: int = 0      # deduped minus already-known
    after_max_results_count: int = 0  # eligible after applying max_results
    details_fetched_count: int = 0    # actual Place Details calls
    inserted_count: int = 0           # rows upserted to DB
    errors: list[str] = field(default_factory=list)
    artifact_path: Path | None = None


class DiscoverController:
    def __init__(
        self,
        gateway: _PlacesGateway,
        repo: GooglePlacesLeadRepository,
        artifacts_dir: Path,
        *,
        queries: tuple[str, ...] = DEFAULT_QUERIES,
    ) -> None:
        self._gateway = gateway
        self._repo = repo
        self._artifacts_dir = Path(artifacts_dir)
        self._queries = queries

    def run(
        self,
        city: str,
        *,
        max_results: int | None = None,
        max_pages_per_query: int = 1,
        run_id: str | None = None,
    ) -> DiscoverResult:
        if not city or not city.strip():
            raise ValueError("city must be non-empty")
        if max_results is not None and max_results < 0:
            raise ValueError("max_results must be >= 0 (or None for unlimited)")
        if max_pages_per_query < 1:
            raise ValueError("max_pages_per_query must be >= 1")

        run_id = run_id or _make_run_id()
        result = DiscoverResult(run_id=run_id, city=city)

        logger.info(
            "discover.start city={city} run_id={run_id} queries={n_queries} "
            "max_results={max_results} max_pages={max_pages}",
            city=city,
            run_id=run_id,
            n_queries=len(self._queries),
            max_results=max_results,
            max_pages=max_pages_per_query,
        )

        deduped = self._discover_candidates(city, max_pages_per_query, result)
        new_place_ids = self._filter_to_new(deduped, result)

        if max_results is not None:
            new_place_ids = new_place_ids[:max_results]
        result.after_max_results_count = len(new_place_ids)

        if new_place_ids:
            leads, artifact_path = self._fetch_and_record(
                city, run_id, new_place_ids, result
            )
            result.artifact_path = artifact_path

            if leads:
                self._repo.upsert_many(leads)
                result.inserted_count = len(leads)

        logger.info(
            "discover.done city={city} run_id={run_id} "
            "inserted={inserted} fetched={fetched} errors={errors}",
            city=city,
            run_id=run_id,
            inserted=result.inserted_count,
            fetched=result.details_fetched_count,
            errors=len(result.errors),
        )
        return result

    # ── pipeline stages ──────────────────────────────────────────────

    def _discover_candidates(
        self, city: str, max_pages_per_query: int, result: DiscoverResult
    ) -> dict[str, Place]:
        """Run all configured queries and return a deduped {place_id: Place} map."""
        deduped: dict[str, Place] = {}
        for query_template in self._queries:
            query = query_template.format(city=city)
            try:
                places = self._gateway.text_search(
                    query, max_pages=max_pages_per_query
                )
            except Exception as e:
                msg = f"text_search failed for {query!r}: {e}"
                logger.error(msg)
                result.errors.append(msg)
                continue

            result.text_search_total += len(places)
            for p in places:
                if p.id not in deduped:
                    deduped[p.id] = p

        result.deduped_total = len(deduped)
        logger.info(
            "discover.dedupe city={city} text_search_total={total} deduped={deduped}",
            city=city,
            total=result.text_search_total,
            deduped=result.deduped_total,
        )
        return deduped

    def _filter_to_new(
        self, deduped: dict[str, Place], result: DiscoverResult
    ) -> list[str]:
        """Skip place_ids already known to the repo (re-run policy)."""
        candidate_ids = list(deduped.keys())
        known = self._repo.exists_many(candidate_ids)
        new_ids = [pid for pid in candidate_ids if pid not in known]

        result.already_known_count = len(known)
        result.new_eligible_count = len(new_ids)
        logger.info(
            "discover.skip already_known={known} new_eligible={new}",
            known=result.already_known_count,
            new=result.new_eligible_count,
        )
        return new_ids

    def _fetch_and_record(
        self,
        city: str,
        run_id: str,
        place_ids: list[str],
        result: DiscoverResult,
    ) -> tuple[list[GooglePlacesLead], Path | None]:
        """Fetch Place Details for each id, build GooglePlacesLeads, and write a
        JSONL artifact of the raw responses. Per-place errors are
        captured on `result.errors`; one bad place doesn't fail the run.

        Returns the artifact path only if at least one lead was written;
        an empty file is cleaned up so we don't leave stale artifacts.
        """
        leads: list[GooglePlacesLead] = []
        artifact_path = self._artifact_path(city, run_id)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)

        total = len(place_ids)
        with artifact_path.open("w", encoding="utf-8") as f:
            for i, pid in enumerate(place_ids, start=1):
                try:
                    raw = self._gateway.get_place_details(pid)
                    lead = google_places_lead_from_place_details(raw, city=city)
                except Exception as e:
                    msg = f"get_place_details failed for {pid}: {e}"
                    logger.error(msg)
                    result.errors.append(msg)
                    continue

                f.write(json.dumps(raw, ensure_ascii=False))
                f.write("\n")

                leads.append(lead)
                result.details_fetched_count += 1

                if i % 20 == 0 or i == total:
                    logger.info(
                        "discover.progress city={city} fetched={fetched}/{total}",
                        city=city,
                        fetched=i,
                        total=total,
                    )

        if result.details_fetched_count == 0:
            artifact_path.unlink(missing_ok=True)
            return [], None
        return leads, artifact_path

    # ── helpers ──────────────────────────────────────────────────────

    def _artifact_path(self, city: str, run_id: str) -> Path:
        return self._artifacts_dir / slugify(city) / f"{run_id}.jsonl"


def _make_run_id() -> str:
    """Sortable run id: YYYYMMDD-HHMMSS-XXXX (4 hex chars of randomness)."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = secrets.token_hex(2)
    return f"{ts}-{suffix}"
