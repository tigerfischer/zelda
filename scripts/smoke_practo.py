"""Manual smoke test for PractoPlaywrightGateway against a live Practo
profile.

Two modes:

  # Mode A: ad-hoc one-shot — give it a Practo URL, see what comes back.
  conda run -n zelda python scripts/smoke_practo.py one \\
      --place-id ChIJ_X \\
      --practo-url "https://www.practo.com/bangalore/doctor/dr-k-a-mohan?practice_id=1138332"

  # Mode B: enrich every pending stub row in the DB.
  conda run -n zelda python scripts/smoke_practo.py pending --max 3

`one` does NOT touch the DB by default — it prints the parsed result
and exits, so you can sanity-check a URL before committing to a stub
row. Add --persist to write the result into the DB.

`pending` always uses the DB.

Implementation note: the gateway runs Chromium in `--headless=new`
mode with two stealth tweaks (`--disable-blink-features=
AutomationControlled` + nulling `navigator.webdriver`). That
combination passes Practo's Akamai challenge from a residential IP.
Pass --headful for visual debugging when the scraper breaks.
"""

import argparse
import json
import sys

from zelda.config import Settings
from zelda.controllers.enrich_practo import EnrichPractoController
from zelda.gateways.practo_playwright import PractoPlaywrightGateway
from zelda.repositories.practo_profile_repo import PractoProfileRepository


def _print_profile(profile, *, show_raw: bool) -> None:
    print(f"  fetch_status            = {profile.fetch_status}")
    print(f"  practo_doctor_id        = {profile.practo_doctor_id}")
    print(f"  name                    = {profile.name!r}")
    print(f"  qualifications          = {profile.qualifications}")
    print(f"  experience_years        = {profile.experience_years}")
    print(f"  specializations         = {profile.specializations}")
    print(f"  languages               = {profile.languages}")
    print(f"  consultation_fee        = {profile.consultation_fee} {profile.consultation_fee_currency or ''}")
    print(f"  recommendation_percent  = {profile.recommendation_percent}")
    print(f"  patient_count           = {profile.patient_count}")
    print(f"  reviews_count           = {profile.reviews_count}")
    print(f"  rating                  = {profile.rating}")
    print(f"  has_practo_plus_badge   = {profile.has_practo_plus_badge}")
    print(f"  next_available_at       = {profile.next_available_at}")
    print(f"  clinic_name             = {profile.clinic_name!r}")
    print(f"  clinic_locality         = {profile.clinic_locality!r}")
    print(f"  clinic_city             = {profile.clinic_city!r}")
    print(f"  clinic_address          = {profile.clinic_address!r}")
    print(f"  lat,lng                 = {profile.lat}, {profile.lng}")
    print(f"  services (count)        = {len(profile.services)}")
    print(f"  photo_urls (count)      = {len(profile.photo_urls)}")
    print(f"  registrations (count)   = {len(profile.registrations)}")
    print(f"  education (count)       = {len(profile.education)}")
    print(f"  awards (count)          = {len(profile.awards)}")
    print(f"  memberships (count)     = {len(profile.memberships)}")
    if profile.summary:
        preview = profile.summary[:200].replace("\n", " ")
        print(f"  summary                 = {preview!r}")
    if show_raw:
        keys = sorted(profile.raw_json.keys())
        print(f"\n  raw_json top-level keys = {keys}")
        prof = profile.raw_json.get("profile_reducer", {})
        if isinstance(prof, dict):
            print(f"  profile_reducer keys    = {sorted(prof.keys())[:20]}...")


def _cmd_one(args, settings: Settings) -> int:
    print(f"Fetching place_id={args.place_id}")
    print(f"Practo URL: {args.practo_url}")
    print(f"Headful={args.headful}  Persist={args.persist}")
    print()

    if args.headful:
        # Manual headful launch path (overrides the headless=new args).
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        gw = PractoPlaywrightGateway(playwright=pw, browser=browser)
    else:
        gw = PractoPlaywrightGateway.launch()

    try:
        with gw:
            result = gw.fetch_profile(
                place_id=args.place_id, practo_url=args.practo_url
            )
    except KeyboardInterrupt:
        print("interrupted")
        return 130

    print(f"\n--- PractoFetchResult ---")
    print(f"  status                  = {result.status}")
    print(f"  final_url               = {result.final_url}")
    if result.error_message:
        print(f"  error_message           = {result.error_message}")

    print(f"\n--- PractoProfile ---")
    _print_profile(result.profile, show_raw=args.show_raw)

    if args.persist:
        with PractoProfileRepository(settings.db_path) as repo:
            repo.upsert_stub(args.place_id, args.practo_url)
            repo.upsert(result.profile)
        print(f"\n  persisted to: {settings.db_path}")

    if args.dump_json:
        print(f"\n--- raw_json ---")
        print(json.dumps(result.profile.raw_json, indent=2, ensure_ascii=False, default=str))

    return 0 if result.status == "ok" else 1


def _cmd_pending(args, settings: Settings) -> int:
    print(f"Enriching up to {args.max} pending Practo stubs from {settings.db_path}")
    print()

    with PractoProfileRepository(settings.db_path) as repo:
        pending = repo.get_pending(limit=args.max)
        if not pending:
            print("No pending stubs in the DB. Add one with:")
            print(
                "  python scripts/smoke_practo.py one "
                "--place-id <id> --practo-url <url> --persist"
            )
            return 0

        print(f"Found {len(pending)} pending stub(s):")
        for p in pending:
            print(f"  - {p.place_id}  →  {p.practo_url}")
        print()

        with PractoPlaywrightGateway.launch() as gw:
            ctrl = EnrichPractoController(
                gw,
                repo,
                inter_lead_seconds=args.delay,
                inter_lead_jitter_seconds=args.jitter,
            )
            result = ctrl.enrich_pending(max_leads=args.max)

    print(f"\n--- EnrichResult ---")
    print(f"  attempted     = {result.n_attempted}")
    print(f"  ok            = {result.n_ok}")
    print(f"  not_found     = {result.n_not_found}")
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

    p_one = sub.add_parser(
        "one",
        help="Fetch one specific Practo URL ad-hoc. Doesn't touch the DB unless --persist.",
    )
    p_one.add_argument("--place-id", required=True, help="The lead's place_id (FK).")
    p_one.add_argument(
        "--practo-url",
        required=True,
        help="Practo profile or clinic URL. Search URLs aren't supported here.",
    )
    p_one.add_argument(
        "--persist", action="store_true",
        help="Upsert a stub row + the result into the DB.",
    )
    p_one.add_argument(
        "--show-raw", action="store_true",
        help="Print the raw_json top-level keys for inspection.",
    )
    p_one.add_argument(
        "--dump-json", action="store_true",
        help="Print the full raw_json (very verbose).",
    )
    p_one.add_argument(
        "--headful", action="store_true",
        help="Show a real browser window for visual debugging.",
    )

    p_pend = sub.add_parser(
        "pending",
        help="Enrich every pending stub row in the DB (or up to --max).",
    )
    p_pend.add_argument("--max", type=int, default=3)
    p_pend.add_argument(
        "--delay", type=float, default=4.0,
        help="Base seconds between Practo fetches (default 4).",
    )
    p_pend.add_argument(
        "--jitter", type=float, default=3.0,
        help="Random extra seconds added to each delay (default 3 — actual gaps 4–7s).",
    )

    args = parser.parse_args(argv)
    settings = Settings()

    if args.cmd == "one":
        return _cmd_one(args, settings)
    if args.cmd == "pending":
        return _cmd_pending(args, settings)
    parser.error(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
