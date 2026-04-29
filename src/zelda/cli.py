"""Zelda CLI.

Subcommands:
    discover  — run the discover controller for a city
    sync      — push DB → Drive for a city

Both subcommands read configuration from `.env` via Settings, wire up
the appropriate gateway + repo + controller, and print a one-line
result summary.
"""

import argparse
import sys

from zelda.config import Settings
from zelda.controllers.discover import DiscoverController, DiscoverResult
from zelda.controllers.sync import DriveSyncController, SyncResult
from zelda.gateways.google_drive import GoogleDriveGateway
from zelda.gateways.google_places import GooglePlacesGateway
from zelda.repositories.raw_lead_repo import RawLeadRepository


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


# ── entry point ─────────────────────────────────────────────────────


_HANDLERS = {
    "discover": cmd_discover,
    "sync": cmd_sync,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = Settings()
    handler = _HANDLERS[args.command]
    return handler(args, settings)


if __name__ == "__main__":
    sys.exit(main())
