"""Zelda CLI.

Subcommands:
    discover       — find dentists in a city via Google Places
    sync           — push DB → Drive for a city
    bootstrap      — pull Drive → fresh local DB (cross-machine setup)
    fetch-reviews  — capture per-place reviews from Google Maps
    enrich         — run all enrichment sources for a city, source-level cached

All subcommands read configuration from `.env` via Settings, wire up
the appropriate gateway + repo + controller, and print a one-line
result summary.
"""

import argparse
import sys

from zelda.config import Settings
from zelda.controllers.bootstrap import BootstrapController, BootstrapResult
from zelda.controllers.discover import DiscoverController, DiscoverResult
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
from zelda.controllers.sync import DriveSyncController, SyncResult
from zelda.gateways.google_drive import GoogleDriveGateway
from zelda.gateways.google_places import GooglePlacesGateway
from zelda.gateways.google_reviews import GoogleReviewsGateway
from zelda.gateways.practo_playwright import PractoPlaywrightGateway
from zelda.repositories.practo_profile_repo import PractoProfileRepository
from zelda.repositories.raw_lead_repo import RawLeadRepository
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
        "discover", help="discover dentists in a city via Google Places"
    )
    p_disc.add_argument("--city", required=True, help="City name, e.g. Ludhiana")
    p_disc.add_argument(
        "--max-results",
        type=_max_results_type,
        default=1,
        help=(
            "Cap on Place Details fetches (the cost driver). "
            "0 = no fetches (dry run); 'all' = unlimited. Default: 1."
        ),
    )
    p_disc.add_argument(
        "--max-pages",
        type=_positive_int,
        default=1,
        help="Pagination depth per text-search query. Default: 1.",
    )

    p_sync = sub.add_parser("sync", help="push DB → Drive for a city")
    p_sync.add_argument("--city", required=True, help="City name, e.g. Ludhiana")

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
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    with GooglePlacesGateway(api_key=settings.google_places_api_key) as gateway:
        repo = RawLeadRepository(settings.db_path)
        try:
            controller = DiscoverController(
                gateway=gateway,
                repo=repo,
                artifacts_dir=settings.raw_artifacts_dir,
            )
            result: DiscoverResult = controller.run(
                args.city,
                max_results=args.max_results,
                max_pages_per_query=args.max_pages,
            )
        finally:
            repo.close()

    print(
        f"discover {args.city}: "
        f"deduped={result.deduped_total} "
        f"new_eligible={result.new_eligible_count} "
        f"after_max_results={result.after_max_results_count} "
        f"fetched={result.details_fetched_count} "
        f"inserted={result.inserted_count} "
        f"errors={len(result.errors)} "
        f"run_id={result.run_id}"
    )
    return 0 if not result.errors else 1


def cmd_sync(args: argparse.Namespace, settings: Settings) -> int:
    drive = GoogleDriveGateway.from_oauth_file(
        settings.google_oauth_client_secrets,
        settings.google_oauth_token_cache,
        settings.google_drive_folder_id,
    )
    repo = RawLeadRepository(settings.db_path)
    try:
        controller = DriveSyncController(
            drive=drive, repo=repo, artifacts_dir=settings.raw_artifacts_dir
        )
        result: SyncResult = controller.sync_city(args.city)
    finally:
        repo.close()

    print(
        f"sync {args.city}: "
        f"unsynced={result.n_unsynced} "
        f"sheet_inserted={result.n_inserted_in_sheet} "
        f"sheet_updated={result.n_updated_in_sheet} "
        f"artifacts_uploaded={result.n_artifacts_uploaded} "
        f"artifacts_skipped={result.n_artifacts_skipped} "
        f"errors={len(result.errors)}"
    )
    return 0 if not result.errors else 1


def cmd_bootstrap(args: argparse.Namespace, settings: Settings) -> int:
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    drive = GoogleDriveGateway.from_oauth_file(
        settings.google_oauth_client_secrets,
        settings.google_oauth_token_cache,
        settings.google_drive_folder_id,
    )
    repo = RawLeadRepository(settings.db_path)
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

    lead_repo = RawLeadRepository(settings.db_path)
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

    lead_repo = RawLeadRepository(settings.db_path)
    review_repo = ReviewRepository(settings.db_path)
    practo_repo = PractoProfileRepository(settings.db_path)

    headless = not args.headful
    reviews_gw = GoogleReviewsGateway.launch(headless=headless)
    practo_gw = PractoPlaywrightGateway.launch()

    try:
        reviews_ctrl = FetchReviewsController(
            gateway=reviews_gw,
            review_repo=review_repo,
            lead_repo=lead_repo,
            artifacts_dir=reviews_artifacts_dir,
        )
        practo_ctrl = EnrichPractoController(
            gateway=practo_gw,
            repo=practo_repo,
        )
        reviews_adapter = GoogleReviewsSourceAdapter(
            controller=reviews_ctrl,
            review_repo=review_repo,
            max_reviews_per_place=args.max_reviews_per_place,
        )
        practo_adapter = PractoSourceAdapter(
            controller=practo_ctrl,
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
        try:
            reviews_gw.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            practo_gw.close()
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
