"""Manual smoke test for GoogleReviewsGateway against a real
Google Maps page.

    conda run -n zelda python scripts/smoke_reviews.py \\
        --place-id ChIJYf2cJ6WCGjkRcGkj6KM0CXE \\
        --search-query "Sai Dental Clinic Ludhiana" \\
        --max-reviews 30

Defaults: Sai Dental Clinic in Ludhiana (no phone, no website,
4.9★ / 272 reviews — the highest-value-jump candidate from
discovery, perfect first target).

Watch the headful browser by passing --headful for visual
debugging when the scraper breaks.
"""

import argparse
import sys
from pathlib import Path

from zelda.gateways.google_reviews import GoogleReviewsGateway


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--place-id",
        default="ChIJYf2cJ6WCGjkRcGkj6KM0CXE",
        help="Place ID (default: Sai Dental Clinic in Ludhiana)",
    )
    parser.add_argument(
        "--search-query",
        default="Sai Dental Clinic Ludhiana",
        help="Maps search string that will land on this place "
             '(default: "Sai Dental Clinic Ludhiana")',
    )
    parser.add_argument(
        "--max-reviews",
        type=int,
        default=30,
        help="Cap on reviews to capture (default: 30 for quick smoke)",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Show browser window (off by default)",
    )
    parser.add_argument(
        "--total-hint",
        type=int,
        default=None,
        help="Pass userRatingCount to populate total_reviews_per_gbp",
    )
    args = parser.parse_args(argv)

    print(f"Fetching up to {args.max_reviews} reviews for place_id={args.place_id}")
    print(f"  search_query={args.search_query!r}")
    print(f"  Headful={args.headful}")
    print()

    with GoogleReviewsGateway.launch(headless=not args.headful) as gw:
        result = gw.fetch_reviews(
            args.place_id,
            search_query=args.search_query,
            max_reviews=args.max_reviews,
            total_reviews_hint=args.total_hint,
        )

    print(f"\n--- ReviewSet ---")
    print(f"  place_id            = {result.place_id}")
    print(f"  fetch_status        = {result.fetch_status}")
    if result.error_message:
        print(f"  error_message       = {result.error_message}")
    print(f"  capture_cap         = {result.capture_cap}")
    print(f"  capture_order       = {result.capture_order}")
    print(f"  reviews_captured    = {result.reviews_captured}")
    print(f"  total_per_gbp       = {result.total_reviews_per_gbp}")
    print(f"  is_truncated        = {result.is_truncated}")
    print(f"  earliest_review_at  = {result.earliest_review_at}")
    print(f"  latest_review_at    = {result.latest_review_at}")
    print(f"  captured_at         = {result.captured_at}")

    if not result.reviews:
        print("\n(no reviews captured)")
        return 1 if result.fetch_status != "ok" else 0

    print(f"\n--- First 3 reviews ---")
    for r in result.reviews[:3]:
        print(f"\n  [{r.sequence_in_capture}] review_id={r.review_id}")
        print(f"    rating              = {r.rating}")
        print(f"    author              = {r.author_name}")
        print(f"    relative_time       = {r.relative_publish_time!r}")
        print(f"    approx_publish_at   = {r.approx_publish_at}")
        text_preview = (r.text or "")[:120].replace("\n", " ")
        print(f"    text (preview)      = {text_preview!r}")
        if r.owner_response_text:
            resp_preview = r.owner_response_text[:120].replace("\n", " ")
            print(f"    owner_response      = {resp_preview!r}")
            print(f"    owner_response_at   = {r.owner_response_relative_time!r}")
        if r.photo_urls:
            print(f"    photo count         = {len(r.photo_urls)}")
        if r.likes_count:
            print(f"    likes               = {r.likes_count}")

    n_with_responses = sum(1 for r in result.reviews if r.owner_response_text)
    print(f"\n  Owner responses found in {n_with_responses}/{len(result.reviews)} reviews")

    return 0


if __name__ == "__main__":
    sys.exit(main())
