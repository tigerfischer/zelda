"""Enrichment source adapters.

Each `SourceAdapter` wraps one per-source controller + repo behind a
uniform Protocol so the `EnrichmentOrchestrator` can iterate over
(lead × source) pairs and only fetch what's missing or stale.

Today: GoogleReviewsSourceAdapter, PractoSourceAdapter.
Tomorrow: WebsiteSourceAdapter, InstagramSourceAdapter, IDARegistry-
SourceAdapter, ... — each one is a thin shim (~50 lines) that
exposes `can_fetch`, `is_cached_fresh`, and `fetch_for_lead`.

Why a Protocol rather than an abstract base class? Each source's
storage shape (fields on its repo, semantics of its `fetch_status`
states) is different. A structural type lets each adapter be the
thinnest possible bridge between a uniform orchestrator and a
source-specific controller, without forcing them to inherit shared
state they don't need.
"""

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from loguru import logger

from zelda.controllers.enrich_practo import EnrichPractoController
from zelda.controllers.fetch_reviews import FetchReviewsController
from zelda.models.google_places_lead import GooglePlacesLead
from zelda.repositories.practo_profile_repo import PractoProfileRepository
from zelda.repositories.review_repo import ReviewRepository


# Status sets the orchestrator interprets uniformly across sources.
# Adapters MUST emit fetch_status strings that fall into exactly one
# of these buckets so the orchestrator can decide its action.

SUCCESSFUL_STATUSES: frozenset[str] = frozenset({"ok", "partial"})
"""Source returned usable data. Counts toward `n_successful`."""

BLOCKED_STATUSES: frozenset[str] = frozenset({"blocked", "captcha"})
"""Source was actively blocked (Akamai, Google /sorry/, etc.). The
orchestrator aborts the run because more requests will hit the same
wall."""

ERROR_STATUSES: frozenset[str] = frozenset({"error"})
"""Source ran but failed for this lead specifically. Continue with
remaining (lead × source) pairs."""

SKIPPED_STATUSES: frozenset[str] = frozenset({
    "skipped", "not_found", "no_url_found",
})
"""Source either couldn't fetch (no prerequisite data) or returned a
terminal "no data exists" signal. Don't retry, don't count as failure.
- `skipped`: defensive — some prerequisite vanished mid-run.
- `not_found`: source fetched but the target was 404 / dead URL.
- `no_url_found`: Practo discovery searched but found no plausible match
  for this lead. Terminal — no point retrying every run."""


@runtime_checkable
class SourceAdapter(Protocol):
    """Each enrichment source implements this interface."""

    @property
    def name(self) -> str: ...

    def can_fetch(self, lead: GooglePlacesLead) -> bool:
        """Does this source have what it needs to fetch this lead?

        Reviews need only `place_id + name + city` → always True.
        Practo needs a known `practo_url` (a stub row) → True only
        when one has been pre-populated by the URL-discovery step.
        """
        ...

    def is_cached_fresh(
        self,
        place_id: str,
        *,
        max_age_days: float,
        now: datetime,
    ) -> bool:
        """Is the cache for this lead valid? True = skip fetch.

        Sources MAY treat certain terminal statuses (e.g. Practo's
        "not_found") as fresh-forever — the URL is dead, no point
        retrying every run.
        """
        ...

    def fetch_for_lead(
        self,
        lead: GooglePlacesLead,
        *,
        capture_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        """Fetch this source's data for `lead`. Persists internally.

        Returns a summary dict that MUST contain at minimum:
            "source": str           # this adapter's `name`
            "place_id": str
            "fetch_status": str     # in SUCCESSFUL/BLOCKED/ERROR/SKIPPED_STATUSES
            "capture_id": str
            "error_message": str | None

        Adapters MAY include additional source-specific keys (e.g.
        `reviews_captured`, `is_truncated`).

        MUST NOT raise on per-lead failures — return an error-status
        dict instead so the orchestrator can decide policy uniformly.
        """
        ...


# ── GoogleReviewsSourceAdapter ──────────────────────────────────────


class GoogleReviewsSourceAdapter:
    """Wraps `FetchReviewsController` + `ReviewRepository` for the
    orchestrator. Reviews can always be fetched given a GooglePlacesLead
    (place_id + name + city is enough), so `can_fetch` is uniformly
    True."""

    name = "google_reviews"

    def __init__(
        self,
        controller: FetchReviewsController,
        review_repo: ReviewRepository,
        *,
        max_reviews_per_place: int = 1000,
    ) -> None:
        self._controller = controller
        self._repo = review_repo
        self._max_reviews_per_place = max_reviews_per_place

    def can_fetch(self, lead: GooglePlacesLead) -> bool:
        return True

    def is_cached_fresh(
        self,
        place_id: str,
        *,
        max_age_days: float,
        now: datetime,
    ) -> bool:
        latest = self._repo.get_latest_capture(place_id)
        if latest is None:
            return False
        if latest["fetch_status"] not in SUCCESSFUL_STATUSES:
            return False
        captured_at = datetime.fromisoformat(latest["captured_at"])
        age_days = (now - captured_at).total_seconds() / 86400
        return age_days < max_age_days

    def fetch_for_lead(
        self,
        lead: GooglePlacesLead,
        *,
        capture_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        summary = self._controller.process_one_lead(
            lead,
            capture_id=capture_id,
            max_reviews=self._max_reviews_per_place,
        )
        summary["source"] = self.name
        return summary


# ── PractoSourceAdapter ─────────────────────────────────────────────


# Practo statuses that mean "we know there's no useful data and
# shouldn't try again every run". Both `not_found` (URL is 404) and
# `no_url_found` (discovery couldn't match a Practo profile to this
# lead) are terminal-fresh-forever from the orchestrator's POV.
_PRACTO_TERMINAL_STATUSES: frozenset[str] = frozenset({
    "not_found", "no_url_found",
})


class PractoSourceAdapter:
    """Wraps Practo PROFILE enrichment for the orchestrator.

    URL discovery (the "which Practo profile, if any, is this lead?"
    decision) is no longer this adapter's job — it's been hoisted into
    a city-wide pre-step (`PractoMatchController`) that runs once per
    `enrich` invocation and populates URL stubs in one batched LLM
    call. By the time this adapter runs, the `practo_profiles` table
    is already in one of the states below.

    Lifecycle of a `practo_profiles` row from this adapter's POV:
      - No row → unexpected (the matcher should have written one);
        treat as `no_url_found` to avoid retrying every run.
      - `pending` row → URL is known but profile not yet fetched;
        run enrichment.
      - `ok` and recent → cache hit (handled in `is_cached_fresh`).
      - `not_found` / `no_url_found` → terminal-fresh-forever; cache
        hit so this method is never called.
      - `blocked` / `error` → retry on next pass.
    """

    name = "practo_profile"

    def __init__(
        self,
        enrich_controller: EnrichPractoController,
        practo_repo: PractoProfileRepository,
    ) -> None:
        self._enrich = enrich_controller
        self._repo = practo_repo

    def can_fetch(self, lead: GooglePlacesLead) -> bool:
        # The pre-step matcher writes a row for EVERY lead in the
        # city — either a `pending` stub (we have a URL) or a
        # `no_url_found` sentinel (we don't). So `can_fetch` is True
        # whenever a row exists with a non-empty practo_url.
        existing = self._repo.get_by_place_id(lead.place_id)
        return existing is not None and bool(existing.practo_url)

    def is_cached_fresh(
        self,
        place_id: str,
        *,
        max_age_days: float,
        now: datetime,
    ) -> bool:
        profile = self._repo.get_by_place_id(place_id)
        if profile is None:
            return False  # no row at all → needs the matcher to run
        if profile.fetch_status in _PRACTO_TERMINAL_STATUSES:
            return True  # don't retry a known-dead/unfindable URL
        if profile.fetch_status != "ok":
            return False  # pending / blocked / error → retry
        if profile.fetched_at is None:
            return False
        age_days = (now - profile.fetched_at).total_seconds() / 86400
        return age_days < max_age_days

    def fetch_for_lead(
        self,
        lead: GooglePlacesLead,
        *,
        capture_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        existing = self._repo.get_by_place_id(lead.place_id)
        if existing is None:
            # Defensive — the pre-step matcher should have written a
            # row. Surface as no_url_found so we don't loop here.
            return {
                "source": self.name,
                "place_id": lead.place_id,
                "fetch_status": "no_url_found",
                "error_message": (
                    "no practo_profiles row for this lead — was the "
                    "discover-practo-urls pre-step skipped?"
                ),
                "capture_id": capture_id,
            }

        if existing.fetch_status in _PRACTO_TERMINAL_STATUSES:
            return {
                "source": self.name,
                "place_id": lead.place_id,
                "fetch_status": existing.fetch_status,
                "error_message": existing.error_message,
                "capture_id": capture_id,
            }

        if not existing.practo_url:
            return {
                "source": self.name,
                "place_id": lead.place_id,
                "fetch_status": "no_url_found",
                "error_message": (
                    f"row exists with status={existing.fetch_status} "
                    "but no practo_url"
                ),
                "capture_id": capture_id,
            }

        try:
            profile = self._enrich.enrich_one(lead.place_id)
        except Exception as e:  # noqa: BLE001
            logger.error(
                "practo_adapter.enrich_exception place_id={pid} err={err}",
                pid=lead.place_id, err=str(e),
            )
            return {
                "source": self.name,
                "place_id": lead.place_id,
                "fetch_status": "error",
                "error_message": (
                    f"enrich exception: {type(e).__name__}: {e}"
                ),
                "capture_id": capture_id,
            }

        if profile is None:
            return {
                "source": self.name,
                "place_id": lead.place_id,
                "fetch_status": "error",
                "error_message": "enrich_one returned None",
                "capture_id": capture_id,
            }

        return {
            "source": self.name,
            "place_id": lead.place_id,
            "fetch_status": profile.fetch_status,
            "error_message": profile.error_message,
            "capture_id": capture_id,
        }
