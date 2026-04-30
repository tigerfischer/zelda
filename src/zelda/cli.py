"""Zelda CLI.

Subcommands:
    discover              — discover dentists across all configured sources
    sync                  — push DB → Drive for a city
    match                 — cross-source matching → unified leads list
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
from zelda.controllers.matching.pipeline import MatchingPipeline, MatchingResult
from zelda.repositories.google_places_lead_repo import GooglePlacesLeadRepository
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

    try:
        pipeline = SyncPipeline(steps=[
            GooglePlacesSyncStep(
                drive=drive, repo=gp_repo, artifacts_dir=settings.raw_artifacts_dir,
            ),
            PractoSyncStep(drive=drive, repo=practo_repo),
            LybrateSyncStep(drive=drive, repo=lybrate_repo),
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
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    reviews_artifacts_dir = settings.data_dir / "reviews-artifacts"

    lead_repo = GooglePlacesLeadRepository(settings.db_path)
    review_repo = ReviewRepository(settings.db_path)
    try:
        with GoogleReviewsGateway.launch(headless=not args.headful) as gateway:
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
            )
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


# ── entry point ─────────────────────────────────────────────────────


_HANDLERS = {
    "discover": cmd_discover,
    "sync": cmd_sync,
    "match": cmd_match,
    "bootstrap": cmd_bootstrap,
    "fetch-reviews": cmd_fetch_reviews,
    "enrich": cmd_enrich,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = Settings()
    handler = _HANDLERS[args.command]
    return handler(args, settings)


if __name__ == "__main__":
    sys.exit(main())
