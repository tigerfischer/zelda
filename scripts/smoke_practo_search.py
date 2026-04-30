"""Manual smoke for PractoSearchGateway + the discovery controller.

Three modes:

  # Mode A: ad-hoc search — inspect what Practo returns for a query
  conda run -n zelda python scripts/smoke_practo_search.py search \\
      --name "Sai Dental Clinic" --city Ludhiana

  # Mode B: score a query against its SERP — see what the controller
  # would do without touching the DB.
  conda run -n zelda python scripts/smoke_practo_search.py score \\
      --name "Sai Dental Clinic" --city Ludhiana

  # Mode C: run discovery for the first N Ludhiana leads from the DB
  # in DRY-RUN mode (default; pass --commit to actually upsert).
  conda run -n zelda python scripts/smoke_practo_search.py ludhiana \\
      --max 5

Implementation note: same Akamai-bypassing Playwright config as the
profile gateway. Use --headful for visual debugging.
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from zelda.config import Settings
from zelda.controllers.discover_practo_urls import (
    DiscoverPractoUrlsController,
    practo_city_slug,
    score_candidate,
)
from zelda.gateways.practo_search import PractoSearchGateway
from zelda.repositories.practo_profile_repo import PractoProfileRepository
from zelda.repositories.raw_lead_repo import RawLeadRepository


def _print_outcome(outcome) -> None:
    print(f"\n--- PractoSearchOutcome ---")
    print(f"  status         = {outcome.status}")
    print(f"  search_url     = {outcome.search_url}")
    print(f"  final_url      = {outcome.final_url}")
    print(f"  candidates     = {len(outcome.candidates)}")
    if outcome.error_message:
        print(f"  error_message  = {outcome.error_message}")
    print()
    for i, c in enumerate(outcome.candidates):
        print(f"  [{i}] {c.doctor_name!r}")
        print(f"      clinic         = {c.clinic_name!r}")
        print(f"      specialization = {c.specialization!r}")
        print(f"      locality       = {c.locality!r}")
        print(f"      verified       = {c.verified_badge}")
        print(f"      url            = {c.practo_url}")


def _cmd_search(args) -> int:
    city_slug = practo_city_slug(args.city)
    print(f"Searching Practo for query={args.name!r} city_slug={city_slug!r}")
    if args.headful:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        gw = PractoSearchGateway(playwright=pw, browser=browser)
    else:
        gw = PractoSearchGateway.launch()
    with gw:
        outcome = gw.search_dentists(
            query=args.name, city_slug=city_slug, max_results=args.max,
        )
    _print_outcome(outcome)
    return 0 if outcome.status == "ok" else 1


def _cmd_score(args) -> int:
    """Same as `search` but adds the controller's score for each
    candidate and shows whether it'd clear `--threshold`."""
    city_slug = practo_city_slug(args.city)
    print(
        f"Searching + scoring: name={args.name!r} city_slug={city_slug!r} "
        f"threshold={args.threshold}"
    )
    with PractoSearchGateway.launch() as gw:
        outcome = gw.search_dentists(
            query=args.name, city_slug=city_slug, max_results=args.max,
        )
    _print_outcome(outcome)
    print(f"\n--- scored against lead name {args.name!r} ---")
    best_score = 0.0
    best_idx = -1
    for i, c in enumerate(outcome.candidates):
        s = score_candidate(args.name, c)
        marker = "✓" if s >= args.threshold else " "
        print(
            f"  {marker} [{i}] score={s:.3f}  "
            f"name={c.doctor_name!r}  clinic={c.clinic_name!r}"
        )
        if s > best_score:
            best_score = s
            best_idx = i
    print()
    if best_idx >= 0 and best_score >= args.threshold:
        print(
            f"WOULD MATCH: candidate[{best_idx}] "
            f"score={best_score:.3f} url={outcome.candidates[best_idx].practo_url}"
        )
    else:
        print(
            f"NO MATCH: best score {best_score:.3f} below threshold "
            f"{args.threshold:.2f}"
        )
    return 0


def _cmd_ludhiana(args) -> int:
    """Run discovery against the first --max Ludhiana leads in the DB."""
    settings = Settings()
    with RawLeadRepository(settings.db_path) as raw_repo:
        leads = raw_repo.get_for_city("Ludhiana")[: args.max]
    if not leads:
        print("No Ludhiana leads in the DB. Run discover/sync first.")
        return 1

    print(f"Selected {len(leads)} Ludhiana leads:")
    for L in leads:
        print(f"  - {L.place_id}  {L.name!r}")
    print()
    print(f"Mode: {'COMMIT' if args.commit else 'DRY-RUN (no upserts)'}")
    print(f"Threshold: {args.threshold}")
    print()

    with PractoProfileRepository(settings.db_path) as profile_repo:
        with PractoSearchGateway.launch() as gw:
            ctrl = DiscoverPractoUrlsController(
                gw, profile_repo,
                inter_search_seconds=args.delay,
                inter_search_jitter_seconds=args.jitter,
            )
            result = ctrl.discover_for_leads(
                leads,
                min_match_score=args.threshold,
                dry_run=not args.commit,
            )

    print(f"\n--- DiscoverPractoUrlsResult ---")
    print(f"  attempted     = {result.n_attempted}")
    print(f"  matched       = {result.n_matched}")
    print(f"  no_match      = {result.n_no_match}")
    print(f"  already_known = {result.n_already_known}")
    print(f"  blocked       = {result.n_blocked}")
    print(f"  error         = {result.n_error}")
    print(f"  stopped_early = {result.stopped_early}")
    if result.errors:
        print(f"\n  errors:")
        for e in result.errors[:5]:
            print(f"    - {e}")

    return 0 if result.n_error == 0 and not result.stopped_early else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_search = sub.add_parser("search", help="One-off Practo search; print SERP")
    p_search.add_argument("--name", required=True, help="Clinic / doctor name")
    p_search.add_argument("--city", required=True, help="City (any case)")
    p_search.add_argument("--max", type=int, default=10)
    p_search.add_argument("--headful", action="store_true")

    p_score = sub.add_parser(
        "score",
        help="Same as `search` plus the controller's score per candidate",
    )
    p_score.add_argument("--name", required=True)
    p_score.add_argument("--city", required=True)
    p_score.add_argument("--max", type=int, default=10)
    p_score.add_argument("--threshold", type=float, default=0.7)

    p_ludhiana = sub.add_parser(
        "ludhiana",
        help="Run discovery for the first --max Ludhiana leads from the DB",
    )
    p_ludhiana.add_argument("--max", type=int, default=5)
    p_ludhiana.add_argument("--threshold", type=float, default=0.7)
    p_ludhiana.add_argument("--delay", type=float, default=4.0)
    p_ludhiana.add_argument("--jitter", type=float, default=3.0)
    p_ludhiana.add_argument(
        "--commit", action="store_true",
        help="Actually upsert rows; default is dry-run.",
    )

    args = parser.parse_args(argv)

    if args.cmd == "search":
        return _cmd_search(args)
    if args.cmd == "score":
        return _cmd_score(args)
    if args.cmd == "ludhiana":
        return _cmd_ludhiana(args)
    parser.error(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
