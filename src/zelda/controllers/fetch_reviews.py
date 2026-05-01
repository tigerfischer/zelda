"""Reviews-fetch controller — orchestrates the reviews gateway + repos
to capture per-place review sets for every dentist in a city.

Pipeline per run:
    list city's leads → filter to "needs refresh" → cap to max_places
      → for each lead:
          - inter-place sleep (rate limit)
          - periodic browser context reset (anti-fingerprint)
          - gateway.fetch_reviews(place_id, search_query, ...)
          - save ReviewSet to repo + write JSONL artifact
          - if blocked: abort the run (don't bash through)

Cost knobs (all configurable):
    `max_places`              — total places this run touches
    `max_reviews_per_place`   — passed to gateway as the cap
    `refresh_min_age_days`    — skip places re-captured within N days
    `force_refresh`           — disable the recency filter

Rate-limit posture:
    Inter-place delay: random uniform in `inter_place_delay_range`
    seconds between place_ids (default 8–20 s). Long enough that a
    Ludhiana-scale city run takes 15–25 minutes of just-resting time
    on top of the actual scrape, which is the price of looking human.

    Browser context reset every `context_reset_interval` places
    (default 20) — flushes accumulated cookies / fingerprint state
    that Google might use to fingerprint the session over time.

Failure handling:
    Per-place gateway failures (timeout, parse errors) are recorded
    on `result.errors` and the run continues. Block detection
    (`fetch_status` ∈ {"captcha", "blocked"}) aborts the run
    immediately — the same block will hit the next place too, so
    burning through is wasteful and worsens our IP reputation.
"""

import json
import random
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from loguru import logger

from zelda.models.google_places_lead import GooglePlacesLead
from zelda.models.review import ReviewSet
from zelda.repositories.google_places_lead_repo import GooglePlacesLeadRepository
from zelda.repositories.review_repo import ReviewRepository
from zelda.util import slugify


class _ReviewsGateway(Protocol):
    """The slice of the reviews gateway that this controller needs.
    Lets tests pass a fake without importing the real Playwright class."""

    def fetch_reviews(
        self,
        place_id: str,
        *,
        search_query: str,
        max_reviews: int = 1000,
        order: str = "newest_first",
        total_reviews_hint: int | None = None,
    ) -> ReviewSet: ...

    def reset_context(self) -> None: ...


@dataclass
class FetchReviewsResult:
    """Summary stats from one run."""

    city: str
    run_id: str
    started_at: datetime
    completed_at: datetime | None = None

    n_leads_in_city: int = 0
    n_skipped_recent: int = 0      # filtered out by refresh_min_age_days
    n_eligible: int = 0            # candidates after recency filter
    n_after_max_places: int = 0    # eligible × min(max_places, len)
    n_processed: int = 0           # actually called gateway for these
    n_successful: int = 0          # fetch_status ∈ {"ok", "partial"}
    n_blocked: int = 0             # fetch_status ∈ {"captcha", "blocked"}
    n_errored: int = 0             # fetch_status == "error" or exception
    n_total_reviews_captured: int = 0
    aborted_due_to_block: bool = False
    captures: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class FetchReviewsController:
    def __init__(
        self,
        gateway: _ReviewsGateway,
        review_repo: ReviewRepository,
        lead_repo: GooglePlacesLeadRepository,
        artifacts_dir: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        inter_place_delay_range: tuple[float, float] = (8.0, 20.0),
        context_reset_interval: int = 20,
    ) -> None:
        self._gateway = gateway
        self._review_repo = review_repo
        self._lead_repo = lead_repo
        self._artifacts_dir = Path(artifacts_dir)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleeper = sleeper or time.sleep
        self._inter_place_delay_range = inter_place_delay_range
        self._context_reset_interval = context_reset_interval

    def run(
        self,
        city: str,
        *,
        max_places: int | None = None,
        max_reviews_per_place: int = 1000,
        refresh_min_age_days: float = 7.0,
        force_refresh: bool = False,
        run_id: str | None = None,
        on_start: Callable[[int], None] | None = None,
        on_progress: Callable[[int, int, str, dict], None] | None = None,
    ) -> FetchReviewsResult:
        if not city or not city.strip():
            raise ValueError("city must be non-empty")
        if max_places is not None and max_places < 0:
            raise ValueError("max_places must be >= 0 (or None for unlimited)")
        if max_reviews_per_place < 1:
            raise ValueError("max_reviews_per_place must be >= 1")
        if refresh_min_age_days < 0:
            raise ValueError("refresh_min_age_days must be >= 0")

        started_at = self._clock()
        run_id = run_id or _make_run_id(started_at)
        result = FetchReviewsResult(
            city=city, run_id=run_id, started_at=started_at,
        )

        logger.info(
            "fetch_reviews.start city={city} run_id={run_id} "
            "max_places={mp} max_reviews_per_place={mr} "
            "refresh_min_age_days={rd} force_refresh={fr}",
            city=city, run_id=run_id,
            mp=max_places, mr=max_reviews_per_place,
            rd=refresh_min_age_days, fr=force_refresh,
        )

        leads = self._lead_repo.get_for_city(city)
        result.n_leads_in_city = len(leads)

        eligible = self._filter_eligible(
            leads, started_at, refresh_min_age_days, force_refresh, result,
        )
        result.n_eligible = len(eligible)

        if max_places is not None:
            eligible = eligible[:max_places]
        result.n_after_max_places = len(eligible)

        if not eligible:
            result.completed_at = self._clock()
            logger.info(
                "fetch_reviews.noop city={city} reason=nothing_eligible",
                city=city,
            )
            return result

        if on_start is not None:
            on_start(len(eligible))

        artifact_dir = self._artifacts_dir / slugify(city)
        artifact_dir.mkdir(parents=True, exist_ok=True)

        for i, lead in enumerate(eligible, start=1):
            # Inter-place rate limit (skip on first iteration)
            if i > 1:
                self._inter_place_sleep()

            # Periodic context reset to flush accumulated cookies
            if (
                self._context_reset_interval > 0
                and (i - 1) > 0
                and (i - 1) % self._context_reset_interval == 0
            ):
                logger.info(
                    "fetch_reviews.reset_context after_place={i}", i=i - 1,
                )
                try:
                    self._gateway.reset_context()
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "fetch_reviews.reset_context_failed err={err}", err=str(e),
                    )

            capture_id = f"{run_id}-{i:04d}"
            summary = self.process_one_lead(
                lead,
                capture_id=capture_id,
                max_reviews=max_reviews_per_place,
                artifact_dir=artifact_dir,
            )

            # Accumulate into the city-level result
            result.n_processed += 1
            result.n_total_reviews_captured += summary["reviews_captured"]
            result.captures.append(summary)
            if summary["error_message"] and summary["fetch_status"] == "error":
                result.errors.append(summary["error_message"])
            for extra in summary.get("extra_errors", []):
                result.errors.append(extra)

            status = summary["fetch_status"]
            if status in {"ok", "partial"}:
                result.n_successful += 1
            elif status in {"captcha", "blocked"}:
                result.n_blocked += 1
                result.aborted_due_to_block = True
            else:  # "error"
                result.n_errored += 1

            logger.info(
                "fetch_reviews.lead_done index={i} place_id={pid} "
                "status={st} captured={n} truncated={tr}",
                i=i, pid=lead.place_id,
                st=status,
                n=summary["reviews_captured"],
                tr=summary["is_truncated"],
            )

            if on_progress is not None:
                on_progress(i, len(eligible), lead.name, summary)

            if result.aborted_due_to_block:
                logger.error(
                    "fetch_reviews.aborted city={city} after_place={i} "
                    "reason=block_signal",
                    city=city, i=i,
                )
                break

        result.completed_at = self._clock()
        logger.info(
            "fetch_reviews.done city={city} run_id={run_id} "
            "processed={p} successful={s} blocked={b} errored={e} "
            "total_reviews={tr}",
            city=city, run_id=run_id,
            p=result.n_processed, s=result.n_successful,
            b=result.n_blocked, e=result.n_errored,
            tr=result.n_total_reviews_captured,
        )
        return result

    # ── pipeline stages ──────────────────────────────────────────────

    def _filter_eligible(
        self,
        leads: list[GooglePlacesLead],
        now: datetime,
        refresh_min_age_days: float,
        force_refresh: bool,
        result: FetchReviewsResult,
    ) -> list[GooglePlacesLead]:
        """Skip leads whose latest capture is < refresh_min_age_days
        old. Bypass when force_refresh is True."""
        if force_refresh:
            return list(leads)

        eligible: list[GooglePlacesLead] = []
        for lead in leads:
            latest = self._review_repo.get_latest_capture(lead.place_id)
            if latest is None:
                eligible.append(lead)
                continue
            last_at = datetime.fromisoformat(latest["captured_at"])
            age_days = (now - last_at).total_seconds() / 86400
            if age_days < refresh_min_age_days:
                result.n_skipped_recent += 1
                continue
            eligible.append(lead)
        return eligible

    def process_one_lead(
        self,
        lead: GooglePlacesLead,
        *,
        capture_id: str,
        max_reviews: int = 1000,
        artifact_dir: Path | None = None,
    ) -> dict[str, Any]:
        """Per-lead entry point. Used both by `run()` (city loop) and
        by the orchestrator's GoogleReviewsSourceAdapter.

        Returns a capture summary dict:
            {
              "place_id": str,
              "fetch_status": str,            # "ok" | "partial" | "blocked" | "captcha" | "error"
              "reviews_captured": int,
              "is_truncated": bool,
              "capture_id": str,
              "artifact_path": str | None,
              "error_message": str | None,
              "extra_errors": list[str],      # repo-save / artifact-write errors
            }

        No side effects on the caller's result object — purely
        compositional. Errors are returned in the dict, not raised.
        """
        if artifact_dir is None:
            artifact_dir = self._artifacts_dir / slugify(lead.city)
        artifact_dir.mkdir(parents=True, exist_ok=True)

        extra_errors: list[str] = []
        search_query = self._build_search_query(lead)
        total_hint = lead.review_count

        try:
            review_set = self._gateway.fetch_reviews(
                lead.place_id,
                search_query=search_query,
                max_reviews=max_reviews,
                total_reviews_hint=total_hint,
            )
        except Exception as e:  # noqa: BLE001
            msg = (
                f"gateway exception for place_id={lead.place_id} "
                f"({type(e).__name__}: {e})"
            )
            logger.error(msg)
            return {
                "place_id": lead.place_id,
                "fetch_status": "error",
                "reviews_captured": 0,
                "is_truncated": False,
                "capture_id": capture_id,
                "artifact_path": None,
                "error_message": msg,
                "extra_errors": [],
            }

        # Persist capture metadata + reviews to SQLite
        try:
            self._review_repo.save_capture(review_set, capture_id=capture_id)
        except Exception as e:  # noqa: BLE001
            msg = (
                f"repo save_capture failed for place_id={lead.place_id}: {e}"
            )
            logger.error(msg)
            extra_errors.append(msg)

        # Write JSONL artifact with one review per line.
        artifact_path: Path | None = None
        if review_set.reviews:
            artifact_path = artifact_dir / f"{capture_id}.jsonl"
            try:
                with artifact_path.open("w", encoding="utf-8") as f:
                    for r in review_set.reviews:
                        f.write(json.dumps(
                            r.model_dump(mode="json"),
                            ensure_ascii=False,
                        ))
                        f.write("\n")
            except Exception as e:  # noqa: BLE001
                msg = (
                    f"artifact write failed for {artifact_path}: {e}"
                )
                logger.error(msg)
                extra_errors.append(msg)
                artifact_path = None

        return {
            "place_id": lead.place_id,
            "fetch_status": review_set.fetch_status,
            "reviews_captured": review_set.reviews_captured,
            "is_truncated": review_set.is_truncated,
            "capture_id": capture_id,
            "artifact_path": str(artifact_path) if artifact_path else None,
            "error_message": review_set.error_message,
            "extra_errors": extra_errors,
        }

    # ── helpers ──────────────────────────────────────────────────────

    def _build_search_query(self, lead: GooglePlacesLead) -> str:
        """Build a Maps-search-friendly query string for this lead.

        Three transforms make Maps search more reliable:
        1. Unicode normalize via NFKD — many GBP names use math-italic
           or sans-serif-bold variants ("𝗦𝗮𝗶" → "Sai") for SEO theatre,
           which trip up the search.
        2. Drop the SEO-spam suffix after the first " - " / " | " /
           " — " separator. GBP names often include things like
           "- Best Dentist Near Me in {city}" tacked on.
        3. Append the city only if it isn't already present in the
           cleaned name.
        """
        return _build_search_query(lead.name, lead.city)

    def _inter_place_sleep(self) -> None:
        low, high = self._inter_place_delay_range
        if high <= low:
            self._sleeper(low)
            return
        self._sleeper(random.uniform(low, high))


def _make_run_id(now: datetime) -> str:
    """Sortable run id: YYYYMMDD-HHMMSS-XXXX."""
    ts = now.strftime("%Y%m%d-%H%M%S")
    suffix = secrets.token_hex(2)
    return f"{ts}-{suffix}"


_NAME_SEPARATORS: tuple[str, ...] = (" - ", " — ", " | ", " :: ", " // ")


def _build_search_query(name: str, city: str) -> str:
    """Pure helper for clinic-name → Maps-search-query transformation.

    Extracted as a module function so it can be unit tested without
    constructing a controller.
    """
    # 1. Unicode normalize so math-italic / sans-serif-bold etc. fall
    # back to plain Latin chars Maps search expects.
    cleaned = unicodedata.normalize("NFKD", name)
    cleaned = "".join(c for c in cleaned if not unicodedata.combining(c))

    # 2. Drop SEO-spam suffix after the first separator
    for sep in _NAME_SEPARATORS:
        if sep in cleaned:
            cleaned = cleaned.split(sep, 1)[0]
            break

    # 3. Collapse internal whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    city = city.strip()
    if not city:
        return cleaned
    if city.lower() in cleaned.lower():
        return cleaned
    return f"{cleaned} {city}"
