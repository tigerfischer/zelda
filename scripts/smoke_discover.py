"""Manual smoke test for the full discover pipeline.

Hits the real Google Places API and writes to the real SQLite DB at
`settings.db_path` (defaults to `data/zelda.db`).

    conda run -n zelda python scripts/smoke_discover.py --city Ludhiana --max-results 1

Cost scales with --max-results:
    --max-results 1  → ~$0.06  (1 details call + ~7 text-search calls)
    --max-results 5  → ~$0.20
    no flag (default 1)  → ~$0.06
"""

import argparse
import sys

from zelda.config import Settings
from zelda.controllers.discover import DiscoverController
from zelda.gateways.google_places import GooglePlacesGateway
from zelda.repositories.raw_lead_repo import RawLeadRepository


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", default="Ludhiana", help="City to discover (default: Ludhiana)")
    parser.add_argument(
        "--max-results",
        type=int,
        default=1,
        help="Cap on Place Details calls. 0 = none. Default: 1.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=1,
        help="Pagination depth per text-search query. Default: 1.",
    )
    args = parser.parse_args(argv)

    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    print(f"Running discovery for city={args.city!r}")
    print(f"  max_results        = {args.max_results}")
    print(f"  max_pages_per_query = {args.max_pages}")
    print(f"  DB path             = {settings.db_path}")
    print(f"  Artifacts dir       = {settings.raw_artifacts_dir}")
    print()

    with GooglePlacesGateway(api_key=settings.google_places_api_key) as gateway:
        repo = RawLeadRepository(settings.db_path)
        try:
            controller = DiscoverController(
                gateway=gateway,
                repo=repo,
                artifacts_dir=settings.raw_artifacts_dir,
            )
            result = controller.run(
                args.city,
                max_results=args.max_results,
                max_pages_per_query=args.max_pages,
            )
            total_in_db = repo.count_for_city(args.city)
        finally:
            repo.close()

    print(f"\nRun ID: {result.run_id}")
    print(f"  text_search_total      = {result.text_search_total}")
    print(f"  deduped_total          = {result.deduped_total}")
    print(f"  already_known_count    = {result.already_known_count}")
    print(f"  new_eligible_count     = {result.new_eligible_count}")
    print(f"  after_max_results      = {result.after_max_results_count}")
    print(f"  details_fetched_count  = {result.details_fetched_count}")
    print(f"  inserted_count         = {result.inserted_count}")
    print(f"  errors                 = {len(result.errors)}")
    for e in result.errors[:5]:
        print(f"    - {e}")
    if len(result.errors) > 5:
        print(f"    … and {len(result.errors) - 5} more")

    print(f"\nArtifact path: {result.artifact_path}")
    if result.artifact_path and result.artifact_path.exists():
        size = result.artifact_path.stat().st_size
        n = sum(1 for _ in result.artifact_path.open(encoding="utf-8"))
        print(f"  ({n} JSONL lines, {size} bytes)")

    print(f"\nTotal {args.city!r} leads in DB now: {total_in_db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
