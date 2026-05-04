"""Zelda CLI.

Subcommands:
    discover              — discover dentists across all configured sources
    sync                  — push DB → Drive for a city
    match                 — cross-source matching → unified leads list
    enrich-leads          — compute enrichment signals + lead scores for a city
    bootstrap             — pull Drive → fresh local DB (cross-machine setup)
    fetch-reviews         — capture per-place reviews from Google Maps
    enrich                — run all enrichment sources for a city (cached)

All subcommands read configuration from `.env` via Settings, wire up
the appropriate gateway + repo + controller, and print a one-line
result summary.

Typical end-to-end flow for a city:
    discover --city CITY    # → per-source tables (google_places, practo, lybrate)
    match --city CITY       # → unified leads table (enriched + standalone)
    sync --city CITY        # → mirror all tables to Drive
"""

import argparse
import sys

from loguru import logger

from zelda.config import Settings
from zelda.controllers.bootstrap import BootstrapController, BootstrapResult
from zelda.controllers.discover import DiscoverController
from zelda.controllers.discovery_pipeline import (
    DiscoveryPipeline,
    PipelineResult,
)
from zelda.controllers.discovery_steps import (
    GooglePlacesDiscoveryStep,
    LybrateDiscoveryStep,
    PractoDiscoveryStep,
)
from zelda.controllers.enrich_practo import EnrichPractoController
from zelda.controllers.enrichment_orchestrator import (
    EnrichmentOrchestrator,
    OrchestratorResult,
)
from zelda.controllers.enrichment_sources import (
    GoogleReviewsSourceAdapter,
    PractoSourceAdapter,
)
from zelda.controllers.fetch_reviews import (
    FetchReviewsController,
    FetchReviewsResult,
)
from zelda.controllers.lybrate_directory import LybrateDirectoryController
from zelda.controllers.practo_directory import PractoDirectoryController
from zelda.controllers.sync_pipeline import SyncPipeline, SyncPipelineResult
from zelda.controllers.sync_steps import (
    EnrichmentSyncStep,
    GooglePlacesSyncStep,
    LybrateSyncStep,
    PractoSyncStep,
)
from zelda.gateways.google_drive import GoogleDriveGateway
from zelda.gateways.google_places import GooglePlacesGateway
from zelda.gateways.google_reviews import GoogleReviewsGateway
from zelda.gateways.lybrate_directory import LybrateDirectoryGateway
from zelda.gateways.practo_directory import PractoDirectoryGateway
from zelda.gateways.practo_playwright import PractoPlaywrightGateway
from zelda.controllers.enrichment.pipeline import EnrichLeadsPipeline, EnrichLeadsResult
from zelda.controllers.matching.pipeline import MatchingPipeline, MatchingResult
from zelda.outreach.whatsapp_personalizer import WhatsAppPersonalizer, lead_context_from_enrichment
from zelda.repositories.google_places_lead_repo import GooglePlacesLeadRepository
from zelda.repositories.lead_enrichment_repo import LeadEnrichmentRepository
from zelda.repositories.lead_repo import LeadRepository
from zelda.repositories.match_pair_repo import MatchPairRepository
from zelda.repositories.lybrate_listing_repo import LybrateListingRepository
from zelda.repositories.practo_listing_repo import PractoListingRepository
from zelda.repositories.practo_profile_repo import PractoProfileRepository
from zelda.repositories.review_repo import ReviewRepository


# ── argument types ──────────────────────────────────────────────────


def _max_results_type(v: str) -> int | None:
    """`--max-results` accepts non-negative ints and the literal 'all'."""
    if v.lower() == "all":
        return None
    try:
        n = int(v)
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"max-results must be 'all' or a non-negative integer, got {v!r}"
        ) from e
    if n < 0:
        raise argparse.ArgumentTypeError(f"max-results must be >= 0, got {n}")
    return n


def _positive_int(v: str) -> int:
    try:
        n = int(v)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {v!r}") from e
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {n}")
    return n


def _non_negative_float(v: str) -> float:
    try:
        f = float(v)
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"must be a non-negative number, got {v!r}"
        ) from e
    if f < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {f}")
    return f


# ── parser ──────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zelda",
        description="Zelda — lead-generation pipeline for dental clinics",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_disc = sub.add_parser(
        "discover",
        help=(
            "discover dentists across all configured sources "
            "(google_places, practo, lybrate)"
        ),
    )
    p_disc.add_argument("--city", required=True, help="City name, e.g. Ludhiana")
    p_disc.add_argument(
        "--sources",
        default=None,
        help=(
            "Comma-separated subset of sources to run. Default: all "
            "registered. Available: google_places, practo, lybrate."
        ),
    )
    p_disc.add_argument(
        "--gp-max-results",
        type=_max_results_type,
        default=1,
        help=(
            "Google Places only — cap on Place Details fetches "
            "(the cost driver). 0 = no fetches (dry run); 'all' = "
            "unlimited. Default: 1."
        ),
    )
    p_disc.add_argument(
        "--gp-max-pages",
        type=_positive_int,
        default=1,
        help=(
            "Google Places only — pagination depth per text-search "
            "query. Default: 1."
        ),
    )

    p_sync = sub.add_parser(
        "sync",
        help=(
            "push DB → Drive for a city across all sources "
            "(google_places, practo, lybrate)"
        ),
    )
    p_sync.add_argument("--city", required=True, help="City name, e.g. Ludhiana")
    p_sync.add_argument(
        "--sources",
        default=None,
        help=(
            "Comma-separated subset of sources to sync. Default: all. "
            "Available: google_places, practo, lybrate."
        ),
    )
    p_sync.add_argument(
        "--watch",
        action="store_true",
        help=(
            "Run continuously, re-syncing every --interval-seconds. "
            "Keeps Drive up-to-date without manual re-triggering. "
            "Default: one-shot."
        ),
    )
    p_sync.add_argument(
        "--interval-seconds",
        type=_positive_int,
        default=60,
        help="Polling interval for --watch mode. Default: 60.",
    )

    p_match = sub.add_parser(
        "match",
        help="cross-source matching → unified leads list (google_places + practo + lybrate)",
    )
    p_match.add_argument("--city", required=True, help="City name, e.g. Ludhiana")
    p_match.add_argument(
        "--geo-radius-km",
        type=_non_negative_float,
        default=1.0,
        help="Geo pre-filter radius in km for GP↔Practo pairs. Default: 1.0.",
    )
    p_match.add_argument(
        "--min-confidence",
        type=_non_negative_float,
        default=0.75,
        help="Proposer minimum confidence to proceed to Reviewer. Default: 0.75.",
    )

    p_enrich_leads = sub.add_parser(
        "enrich-leads",
        help="compute enrichment signals + lead scores for a city",
    )
    p_enrich_leads.add_argument("--city", required=True, help="City name, e.g. Ludhiana")
    p_enrich_leads.add_argument(
        "--passes",
        default="0,1,2,3,5",
        help="Comma-separated pass numbers to run (default: 0,1,2,3,5). "
             "Pass 4 (photo vision) not yet implemented.",
    )
    p_enrich_leads.add_argument(
        "--force",
        action="store_true",
        help="Re-run passes even if already completed for a lead.",
    )

    p_boot = sub.add_parser(
        "bootstrap",
        help="pull Drive → fresh local DB (cross-machine setup)",
    )
    p_boot.add_argument("--city", required=True, help="City name, e.g. Ludhiana")

    p_rev = sub.add_parser(
        "fetch-reviews",
        help="capture per-place reviews from Google Maps via Playwright",
    )
    p_rev.add_argument("--city", required=True, help="City name, e.g. Ludhiana")
    p_rev.add_argument(
        "--max-places",
        type=_max_results_type,
        default=1,
        help=(
            "Cap on places this run captures reviews for (cost knob). "
            "0 = no fetches; 'all' = unlimited. Default: 1."
        ),
    )
    p_rev.add_argument(
        "--max-reviews-per-place",
        type=_positive_int,
        default=100,
        help=(
            "Cap on reviews captured per place. Default: 100 (a useful "
            "subset for V1; bump to 1000 for full capture)."
        ),
    )
    p_rev.add_argument(
        "--refresh-min-age-days",
        type=_non_negative_float,
        default=7.0,
        help=(
            "Skip places whose latest capture is younger than this many "
            "days. Default: 7. Pass 0 (or use --force-refresh) to disable."
        ),
    )
    p_rev.add_argument(
        "--force-refresh",
        action="store_true",
        help="Re-capture every place regardless of recency.",
    )
    p_rev.add_argument(
        "--headful",
        action="store_true",
        help="Show the browser window (off by default; useful for debug).",
    )

    p_enr = sub.add_parser(
        "enrich",
        help=(
            "run all enrichment sources for a city, with source-level "
            "caching (skip re-fetch when fresh data exists)"
        ),
    )
    p_enr.add_argument("--city", required=True, help="City name, e.g. Ludhiana")
    p_enr.add_argument(
        "--max-leads",
        type=_max_results_type,
        default=1,
        help=(
            "Cap on leads this run touches (cost knob). 0 = no fetches; "
            "'all' = unlimited. Default: 1."
        ),
    )
    p_enr.add_argument(
        "--max-age-days",
        type=_non_negative_float,
        default=180.0,
        help=(
            "Source-level cache window — skip a (lead × source) pair if "
            "we already have a successful capture younger than this many "
            "days. Default: 180 (6 months)."
        ),
    )
    p_enr.add_argument(
        "--force-refresh",
        action="store_true",
        help="Bypass the cache window — re-fetch every (lead × source).",
    )
    p_enr.add_argument(
        "--max-reviews-per-place",
        type=_positive_int,
        default=1000,
        help=(
            "Per-place cap for the Google reviews source (passed through "
            "to the underlying gateway). Default: 1000."
        ),
    )
    p_enr.add_argument(
        "--sources",
        default=None,
        help=(
            "Comma-separated list of sources to run. Default: all "
            "registered. Available: google_reviews, practo_profile."
        ),
    )
    p_enr.add_argument(
        "--headful",
        action="store_true",
        help="Show browser windows (off by default; useful for debug).",
    )

    p_load = sub.add_parser(
        "load-outreach",
        help="Load a generate-outreach JSONL file into the Telegram review queue",
    )
    p_load.add_argument("--file", required=True, help="Path to the JSONL file")

    p_tg = sub.add_parser(
        "telegram-bot",
        help="Run the Telegram bot (message review, call reminders, reply alerts)",
    )

    p_out = sub.add_parser(
        "generate-outreach",
        help=(
            "generate personalized WhatsApp first messages for every lead "
            "in a city, using Claude Haiku — output saved to JSONL"
        ),
    )
    p_out.add_argument("--city", required=True, help="City name, e.g. Ludhiana")
    p_out.add_argument(
        "--max-leads",
        type=_max_results_type,
        default=None,
        help="Cap on leads processed this run. 'all' or omit = unlimited.",
    )
    p_out.add_argument(
        "--output",
        default=None,
        help=(
            "Path to write JSONL output. Default: "
            "data/outreach/{city}/messages_{run_id}.jsonl"
        ),
    )

    return parser


# ── command handlers ────────────────────────────────────────────────


def cmd_discover(args: argparse.Namespace, settings: Settings) -> int:
    """Run the discovery pipeline for `--city`.

    Wires up:
      - GooglePlacesDiscoveryStep    → google_places_leads table
      - PractoDiscoveryStep          → practo_listings table
      - LybrateDiscoveryStep         → lybrate_listings table

    Each step is independent: a failure or block in one source does
    not abort the others. The pipeline returns aggregate counts plus
    per-step breakdowns. Cross-source linking is a separate phase
    (not yet built).
    """
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    only_steps: list[str] | None = None
    if args.sources:
        only_steps = [s.strip() for s in args.sources.split(",") if s.strip()]

    # Open every source's repo + gateway. Resource management here
    # is straightforward — three separate sqlite connections, one
    # httpx client per directory gateway, one Places HTTP client.
    gp_repo = GooglePlacesLeadRepository(settings.db_path)
    practo_repo = PractoListingRepository(settings.db_path)
    lybrate_repo = LybrateListingRepository(settings.db_path)
    practo_gw = PractoDirectoryGateway()
    lybrate_gw = LybrateDirectoryGateway()

    try:
        with GooglePlacesGateway(api_key=settings.google_places_api_key) as gp_gw:
            gp_step = GooglePlacesDiscoveryStep(
                controller=DiscoverController(
                    gateway=gp_gw,
                    repo=gp_repo,
                    artifacts_dir=settings.raw_artifacts_dir,
                ),
                max_results=args.gp_max_results,
                max_pages_per_query=args.gp_max_pages,
            )
            practo_step = PractoDiscoveryStep(
                controller=PractoDirectoryController(
                    gateway=practo_gw, repo=practo_repo,
                ),
            )
            lybrate_step = LybrateDiscoveryStep(
                controller=LybrateDirectoryController(
                    gateway=lybrate_gw, repo=lybrate_repo,
                ),
            )
            pipeline = DiscoveryPipeline(
                steps=[gp_step, practo_step, lybrate_step],
            )
            result: PipelineResult = pipeline.run(
                args.city, only_steps=only_steps,
            )
    finally:
        practo_gw.close()
        lybrate_gw.close()
        gp_repo.close()
        practo_repo.close()
        lybrate_repo.close()

    # Top-level summary + per-step breakdown
    print(
        f"discover {args.city}: "
        f"steps_ran={len(result.by_step)} "
        f"discovered={result.total_discovered} "
        f"inserted={result.total_inserted} "
        f"errors={len(result.step_errors)} "
        f"aborted_steps={[s.step_name for s in result.by_step.values() if s.aborted]} "
        f"skipped={result.skipped_steps} "
        f"run_id={result.run_id}"
    )
    for name, step_result in result.by_step.items():
        print(
            f"  [{name}] "
            f"discovered={step_result.discovered} "
            f"inserted={step_result.inserted} "
            f"already_known={step_result.already_known} "
            f"errors={len(step_result.errors)} "
            f"aborted={step_result.aborted}"
        )

    if result.any_errors():
        return 1
    if result.any_aborted():
        return 2
    return 0


def cmd_sync(args: argparse.Namespace, settings: Settings) -> int:
    """Sync all per-source DB tables to Drive for --city.

    Wires up three `SyncStep` instances (google_places, practo, lybrate)
    into a `SyncPipeline`. Each step independently pushes its unsynced
    rows to `{root}/{City}/discovery/{source}` and marks them synced
    only after the Drive write succeeds (at-least-once delivery).

    In --watch mode the pipeline polls every --interval-seconds, picking
    up rows written by discovery/enrichment without manual re-triggering.
    """
    import time

    only_steps: list[str] | None = None
    if args.sources:
        only_steps = [s.strip() for s in args.sources.split(",") if s.strip()]

    drive = GoogleDriveGateway.from_oauth_file(
        settings.google_oauth_client_secrets,
        settings.google_oauth_token_cache,
        settings.google_drive_folder_id,
    )
    gp_repo = GooglePlacesLeadRepository(settings.db_path)
    practo_repo = PractoListingRepository(settings.db_path)
    lybrate_repo = LybrateListingRepository(settings.db_path)
    enrichment_repo_sync = LeadEnrichmentRepository(settings.db_path)

    try:
        pipeline = SyncPipeline(steps=[
            GooglePlacesSyncStep(
                drive=drive, repo=gp_repo, artifacts_dir=settings.raw_artifacts_dir,
            ),
            PractoSyncStep(drive=drive, repo=practo_repo),
            LybrateSyncStep(drive=drive, repo=lybrate_repo),
            EnrichmentSyncStep(drive=drive, enrichment_repo=enrichment_repo_sync),
        ])

        def _run_once() -> int:
            result: SyncPipelineResult = pipeline.run(args.city, only_steps=only_steps)
            _print_sync_result(result)
            return 0 if not result.any_errors() else 1

        if args.watch:
            logger.info(
                "sync.watch city={c} interval={i}s sources={s}",
                c=args.city, i=args.interval_seconds, s=only_steps or "all",
            )
            while True:
                _run_once()
                time.sleep(args.interval_seconds)
        else:
            return _run_once()
    finally:
        gp_repo.close()
        practo_repo.close()
        lybrate_repo.close()
        enrichment_repo_sync.close()

    return 0  # unreachable in watch mode, satisfies type checkers


def _print_sync_result(result: SyncPipelineResult) -> None:
    print(
        f"sync {result.city}: "
        f"run_id={result.run_id} "
        f"steps_ran={len(result.by_step)} "
        f"pulled={result.total_pulled} "
        f"inserted={result.total_inserted} "
        f"updated={result.total_updated} "
        f"errors={len(result.step_errors)} "
        f"aborted={result.any_aborted()}"
    )
    for name, step in result.by_step.items():
        gp_extras = (
            f" artifacts_uploaded={step.extras.get('artifacts_uploaded', 0)}"
            f" artifacts_skipped={step.extras.get('artifacts_skipped', 0)}"
            if name == "google_places" else ""
        )
        print(
            f"  [{name}] "
            f"pulled={step.pulled} "
            f"inserted={step.inserted} "
            f"updated={step.updated}"
            f"{gp_extras} "
            f"errors={len(step.errors)} "
            f"aborted={step.aborted}"
        )


def cmd_match(args: argparse.Namespace, settings: Settings) -> int:
    """Run cross-source matching for --city.

    Loads all rows from google_places_leads, practo_listings, lybrate_listings,
    runs the pre-filter → Proposer LLM → Reviewer LLM → match graph → Synthesis
    LLM pipeline, and writes unified leads to the `leads` table.

    Requires ANTHROPIC_API_KEY in .env.
    """
    import anthropic as _anthropic

    if not settings.anthropic_api_key:
        print("ERROR: ANTHROPIC_API_KEY is not set in .env", file=sys.stderr)
        return 1

    client = _anthropic.Anthropic(api_key=settings.anthropic_api_key)

    gp_repo = GooglePlacesLeadRepository(settings.db_path)
    practo_repo = PractoListingRepository(settings.db_path)
    lybrate_repo = LybrateListingRepository(settings.db_path)
    lead_repo = LeadRepository(settings.db_path)
    pair_repo = MatchPairRepository(settings.db_path)

    try:
        pipeline = MatchingPipeline(
            gp_repo=gp_repo,
            practo_repo=practo_repo,
            lybrate_repo=lybrate_repo,
            lead_repo=lead_repo,
            pair_repo=pair_repo,
            anthropic_client=client,
            geo_radius_km=args.geo_radius_km,
            proposer_min_confidence=args.min_confidence,
        )
        result: MatchingResult = pipeline.run(args.city)
    finally:
        gp_repo.close()
        practo_repo.close()
        lybrate_repo.close()
        lead_repo.close()
        pair_repo.close()

    print(result.summary())
    print(
        f"  enriched={result.enriched_leads} "
        f"standalone={result.standalone_leads} "
        f"total={result.total_leads} "
        f"human_review={result.human_review_needed}"
    )
    return 0 if not result.errors else 1


def cmd_bootstrap(args: argparse.Namespace, settings: Settings) -> int:
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    drive = GoogleDriveGateway.from_oauth_file(
        settings.google_oauth_client_secrets,
        settings.google_oauth_token_cache,
        settings.google_drive_folder_id,
    )
    repo = GooglePlacesLeadRepository(settings.db_path)
    try:
        controller = BootstrapController(
            drive=drive, repo=repo, artifacts_dir=settings.raw_artifacts_dir
        )
        result: BootstrapResult = controller.bootstrap_city(args.city)
    finally:
        repo.close()

    print(
        f"bootstrap {args.city}: "
        f"drive_files={result.n_drive_artifacts} "
        f"downloaded={result.n_files_downloaded} "
        f"skipped_local={result.n_files_skipped_local} "
        f"processed={result.n_files_processed} "
        f"lines={result.n_lines_total} "
        f"failed_lines={result.n_lines_failed} "
        f"leads_upserted={result.n_leads_upserted} "
        f"errors={len(result.errors)}"
    )
    return 0 if not result.errors else 1


def cmd_fetch_reviews(args: argparse.Namespace, settings: Settings) -> int:
    from zelda.progress import ProgressTracker
    from zelda.util import slugify

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    reviews_artifacts_dir = settings.data_dir / "reviews-artifacts"

    lead_repo = GooglePlacesLeadRepository(settings.db_path)
    review_repo = ReviewRepository(settings.db_path)
    try:
        with GoogleReviewsGateway.launch(headless=not args.headful) as gateway:
            run_id = f"fetch-reviews-{args.city.lower()}-{__import__('secrets').token_hex(4)}"
            tracker = ProgressTracker(
                job="fetch-reviews",
                city=args.city,
                run_id=run_id,
                status_path=settings.data_dir / "progress" / f"fetch-reviews-{slugify(args.city)}.json",
            )
            controller = FetchReviewsController(
                gateway=gateway,
                review_repo=review_repo,
                lead_repo=lead_repo,
                artifacts_dir=reviews_artifacts_dir,
            )
            result: FetchReviewsResult = controller.run(
                args.city,
                max_places=args.max_places,
                max_reviews_per_place=args.max_reviews_per_place,
                refresh_min_age_days=args.refresh_min_age_days,
                force_refresh=args.force_refresh,
                on_start=tracker.set_total,
                on_progress=lambda i, total, name, summary: tracker.update(
                    name=name,
                    status=summary["fetch_status"],
                    reviews=summary["reviews_captured"],
                ),
            )
            tracker.finish(blocked=result.aborted_due_to_block)
    finally:
        review_repo.close()
        lead_repo.close()

    print(
        f"fetch-reviews {args.city}: "
        f"leads={result.n_leads_in_city} "
        f"skipped_recent={result.n_skipped_recent} "
        f"eligible={result.n_eligible} "
        f"after_max_places={result.n_after_max_places} "
        f"processed={result.n_processed} "
        f"successful={result.n_successful} "
        f"blocked={result.n_blocked} "
        f"errored={result.n_errored} "
        f"reviews_captured={result.n_total_reviews_captured} "
        f"aborted_due_to_block={result.aborted_due_to_block} "
        f"errors={len(result.errors)} "
        f"run_id={result.run_id}"
    )
    if result.errors:
        return 1
    if result.aborted_due_to_block:
        return 2
    return 0


def cmd_enrich(args: argparse.Namespace, settings: Settings) -> int:
    """Run all enrichment sources for a city via the orchestrator.

    Wires up:
      - GoogleReviewsGateway + ReviewRepository + FetchReviewsController
        + GoogleReviewsSourceAdapter
      - PractoPlaywrightGateway + PractoProfileRepository
        + EnrichPractoController + PractoSourceAdapter
      - EnrichmentOrchestrator with both adapters

    The orchestrator's source-level cache (`max_age_days`, default 180)
    means re-running with no new leads is cheap.
    """
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    reviews_artifacts_dir = settings.data_dir / "reviews-artifacts"

    only_sources: list[str] | None = None
    if args.sources:
        only_sources = [s.strip() for s in args.sources.split(",") if s.strip()]

    lead_repo = GooglePlacesLeadRepository(settings.db_path)
    review_repo = ReviewRepository(settings.db_path)
    practo_repo = PractoProfileRepository(settings.db_path)

    headless = not args.headful
    # One Playwright runtime shared across both gateways (reviews +
    # practo profile enrich). Sync API only permits one runtime per
    # process, so each gateway accepts an injected pw via
    # `playwright=...` and skips stopping it on close.
    from playwright.sync_api import sync_playwright as _sync_playwright

    pw = _sync_playwright().start()
    reviews_gw = GoogleReviewsGateway.launch(headless=headless, playwright=pw)
    practo_enrich_gw = PractoPlaywrightGateway.launch(playwright=pw)

    try:
        reviews_ctrl = FetchReviewsController(
            gateway=reviews_gw,
            review_repo=review_repo,
            lead_repo=lead_repo,
            artifacts_dir=reviews_artifacts_dir,
        )
        practo_enrich_ctrl = EnrichPractoController(
            gateway=practo_enrich_gw,
            repo=practo_repo,
        )
        reviews_adapter = GoogleReviewsSourceAdapter(
            controller=reviews_ctrl,
            review_repo=review_repo,
            max_reviews_per_place=args.max_reviews_per_place,
        )
        practo_adapter = PractoSourceAdapter(
            enrich_controller=practo_enrich_ctrl,
            practo_repo=practo_repo,
        )
        orchestrator = EnrichmentOrchestrator(
            sources=[reviews_adapter, practo_adapter],
            lead_repo=lead_repo,
        )
        result: OrchestratorResult = orchestrator.enrich_city(
            args.city,
            only_sources=only_sources,
            max_leads=args.max_leads,
            max_age_days=args.max_age_days,
            force_refresh=args.force_refresh,
        )
    finally:
        for gw in (reviews_gw, practo_enrich_gw):
            try:
                gw.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            pw.stop()
        except Exception:  # noqa: BLE001
            pass
        practo_repo.close()
        review_repo.close()
        lead_repo.close()

    # Print top-level summary + per-source breakdown
    print(
        f"enrich {args.city}: "
        f"leads={result.n_leads_in_city} "
        f"after_max_leads={result.n_after_max_leads} "
        f"blocked_sources={result.blocked_sources} "
        f"errors={len(result.errors)} "
        f"run_id={result.run_id}"
    )
    for name, stats in result.by_source.items():
        print(
            f"  [{name}] "
            f"cache_hits={stats.n_cache_hits} "
            f"no_prereq={stats.n_no_prereq} "
            f"skipped_blocked={stats.n_skipped_blocked_earlier} "
            f"attempted={stats.n_attempted} "
            f"successful={stats.n_successful} "
            f"errored={stats.n_errored} "
            f"blocked={stats.n_blocked} "
            f"other_terminal={stats.n_other_terminal}"
        )

    if result.errors:
        return 1
    if result.blocked_sources:
        return 2
    return 0


def cmd_enrich_leads(args: argparse.Namespace, settings: Settings) -> int:
    """Run lead enrichment passes for --city.

    Reads from the `leads` table (produced by `match`), computes
    enrichment signals across Passes 0–3 and 5, and writes results to
    the `lead_enrichments` table.

    Passes:
      0 — existing DB data (free, instant)
      1 — full review history signals (ReviewRepository + LLM Haiku)
      2 — website audit (HTTP + LLM Haiku)
      3 — Practo signals (practo_listings + practo_profiles)
      5 — lead scoring (pure computation)

    Pass 1 and 2 make LLM calls — requires ANTHROPIC_API_KEY in .env.
    """
    import anthropic as _anthropic

    passes_raw = [p.strip() for p in args.passes.split(",") if p.strip()]
    try:
        passes = {int(p) for p in passes_raw}
    except ValueError:
        print(
            f"ERROR: --passes must be comma-separated integers, got {args.passes!r}",
            file=sys.stderr,
        )
        return 1

    # LLM client is optional — Pass 0, 3, 5 don't need it.
    client = None
    if settings.anthropic_api_key and (passes & {1, 2}):
        client = _anthropic.Anthropic(api_key=settings.anthropic_api_key)
    elif passes & {1, 2}:
        print(
            "WARNING: ANTHROPIC_API_KEY not set — Passes 1 and 2 will skip LLM calls.",
            file=sys.stderr,
        )

    from zelda.gateways.website_audit import WebsiteAuditGateway
    from zelda.repositories.review_repo import ReviewRepository

    lead_repo = LeadRepository(settings.db_path)
    enrichment_repo = LeadEnrichmentRepository(settings.db_path)
    gp_repo = GooglePlacesLeadRepository(settings.db_path)
    practo_repo = PractoListingRepository(settings.db_path)
    lybrate_repo = LybrateListingRepository(settings.db_path)
    review_repo = ReviewRepository(settings.db_path)
    website_gw = WebsiteAuditGateway()

    try:
        pipeline = EnrichLeadsPipeline(
            db_path=settings.db_path,
            lead_repo=lead_repo,
            enrichment_repo=enrichment_repo,
            gp_repo=gp_repo,
            practo_repo=practo_repo,
            lybrate_repo=lybrate_repo,
            review_repo=review_repo,
            anthropic_client=client,
            website_gateway=website_gw,
        )
        result: EnrichLeadsResult = pipeline.run(
            args.city,
            passes=passes,
            force=args.force,
        )
    finally:
        lead_repo.close()
        enrichment_repo.close()
        gp_repo.close()
        practo_repo.close()
        lybrate_repo.close()
        review_repo.close()
        website_gw.close()

    print(result.summary())
    for pass_n, count in sorted(result.passes_run.items()):
        print(f"  pass{pass_n}: {count} leads processed")
    if result.errors:
        for e in result.errors[:10]:
            print(f"  ERROR: {e}", file=sys.stderr)

    return 0 if not result.errors else 1


def cmd_load_outreach(args: argparse.Namespace, settings: Settings) -> int:
    """Load a generate-outreach JSONL into outreach_messages (status=pending_review).

    Each record in the file becomes a row in outreach_messages ready for
    the Telegram bot to pick up and send for review.
    """
    import json
    import uuid
    from datetime import datetime, timezone
    from pathlib import Path

    from zelda.models.outreach_message import OutreachMessage
    from zelda.repositories.outreach_repo import OutreachRepository

    path = Path(args.file)
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        return 1

    repo = OutreachRepository(settings.db_path)
    n_loaded = 0
    n_skipped = 0
    n_no_phone = 0

    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            existing = repo.get_by_lead(record["lead_id"])
            if existing and existing.status not in ("skipped",):
                n_skipped += 1
                continue
            phone = record.get("phone") or ""
            if not phone.strip():
                n_no_phone += 1
                continue
            msg = OutreachMessage(
                id=str(uuid.uuid4()),
                lead_id=record["lead_id"],
                clinic_name=record["clinic_name"],
                city=record["city"],
                phone=phone,
                message=record["message"],
                status="pending_review",
                created_at=datetime.now(timezone.utc),
            )
            repo.upsert(msg)
            n_loaded += 1

    repo.close()
    print(
        f"Loaded {n_loaded} messages into review queue "
        f"({n_skipped} already in pipeline, {n_no_phone} skipped — no phone number)."
    )
    return 0


def cmd_telegram_bot(args: argparse.Namespace, settings: Settings) -> int:
    """Start the Telegram bot. Runs until interrupted."""
    import asyncio
    from zelda.outreach.telegram_bot import run_bot
    asyncio.run(run_bot(settings))
    return 0


def cmd_generate_outreach(args: argparse.Namespace, settings: Settings) -> int:
    """Generate a personalized WhatsApp first message for every lead in a city.

    For each lead that has an enrichment record:
      1. Build a LeadContext from the enrichment signals
      2. Call Claude Haiku (WhatsAppPersonalizer) to produce a tailored message
      3. Write one JSON line to the output file

    Output JSONL fields: lead_id, clinic_name, city, phone, message, generated_at

    The output file is safe to review and edit before any sending step.
    """
    import json
    import time
    from datetime import datetime, timezone
    from pathlib import Path

    import anthropic

    city = args.city.strip()
    if not city:
        print("--city must be non-empty", file=sys.stderr)
        return 1

    settings.data_dir.mkdir(parents=True, exist_ok=True)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if args.output:
        output_path = Path(args.output)
    else:
        out_dir = settings.data_dir / "outreach" / city.lower().replace(" ", "_")
        out_dir.mkdir(parents=True, exist_ok=True)
        output_path = out_dir / f"messages_{run_id}.jsonl"

    lead_repo = LeadRepository(settings.db_path)
    enrichment_repo = LeadEnrichmentRepository(settings.db_path)

    try:
        leads = lead_repo.get_for_city(city)
    finally:
        lead_repo.close()

    if not leads:
        print(f"No leads found for city={city!r}. Run 'match --city {city}' first.")
        return 1

    if args.max_leads is not None:
        leads = leads[: args.max_leads]

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    personalizer = WhatsAppPersonalizer(client)

    n_total = len(leads)
    n_ok = 0
    n_skip = 0
    n_err = 0

    print(
        f"generate-outreach  city={city}  leads={n_total}  output={output_path}\n"
    )

    with output_path.open("w", encoding="utf-8") as fout:
        for i, lead in enumerate(leads, start=1):
            enrichment = enrichment_repo.get(lead.lead_id)
            if enrichment is None:
                logger.warning(
                    "generate_outreach.no_enrichment lead_id={lid}", lid=lead.lead_id
                )
                n_skip += 1
                continue

            ctx = lead_context_from_enrichment(enrichment)
            try:
                message = personalizer.personalize(ctx)
                n_ok += 1
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "generate_outreach.error lead_id={lid} err={e}",
                    lid=lead.lead_id, e=e,
                )
                n_err += 1
                continue

            record = {
                "lead_id": lead.lead_id,
                "clinic_name": lead.name,
                "city": city,
                "phone": lead.phone,
                "message": message,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
            fout.write(json.dumps(record, ensure_ascii=False) + "\n")
            fout.flush()

            print(f"[{i:4d}/{n_total}]  {lead.name[:55]}")

            # Polite inter-call pause to avoid Anthropic rate-limit bursts
            if i < n_total:
                time.sleep(0.3)

    enrichment_repo.close()

    print(
        f"\ndone  ok={n_ok}  skipped={n_skip}  errors={n_err}\n"
        f"output: {output_path}"
    )
    return 0 if n_err == 0 else 1


# ── entry point ─────────────────────────────────────────────────────


_HANDLERS = {
    "discover": cmd_discover,
    "sync": cmd_sync,
    "match": cmd_match,
    "enrich-leads": cmd_enrich_leads,
    "bootstrap": cmd_bootstrap,
    "fetch-reviews": cmd_fetch_reviews,
    "enrich": cmd_enrich,
    "generate-outreach": cmd_generate_outreach,
    "load-outreach": cmd_load_outreach,
    "telegram-bot": cmd_telegram_bot,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = Settings()
    handler = _HANDLERS[args.command]
    return handler(args, settings)


if __name__ == "__main__":
    sys.exit(main())
