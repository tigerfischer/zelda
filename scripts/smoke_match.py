"""Smoke test: run the matching pipeline on 8 near-certain GP↔Practo pairs.

Queries the DB for specific rows, builds a mini pipeline with only those rows,
then prints per-pair judgements and the final lead summary.

Usage:
    python scripts/smoke_match.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow running from repo root without installing the package.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import math

import anthropic
from dotenv import load_dotenv

load_dotenv()

from zelda.controllers.matching.llm_judge import LLMJudge
from zelda.controllers.matching.prefilter import CandidatePair
from zelda.models.matchable_row import MatchableRow, from_google_places, from_practo
from zelda.repositories.google_places_lead_repo import GooglePlacesLeadRepository
from zelda.repositories.match_pair_repo import MatchPairRepository
from zelda.repositories.practo_listing_repo import PractoListingRepository

DB_PATH = Path(__file__).parent.parent / "data" / "zelda.db"


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))

# The 8 near-certain pairs (within 10m of each other).
OBVIOUS_PAIRS = [
    ("ChIJeR9oTRKDGjkRb6wOG6gMPpg", "https://www.practo.com/ludhiana/clinic/mittal-dental-and-children-clinic-new-hargobind-nagar"),
    ("ChIJadtYdNODGjkRC6u5hPn3UAQ", "https://www.practo.com/ludhiana/clinic/the-brace-place-brs-nagar"),
    ("ChIJ472wrv6CGjkRk7x3ZjIyCP0", "https://www.practo.com/ludhiana/clinic/aggarwal-dental-clinic-implant-centre-millerganj"),
    ("ChIJ0-uYz3GDGjkRaLR_k-Ya55o", "https://www.practo.com/ludhiana/clinic/k-g-n-dental-lounge-implant-centre-sunder-nagar"),
    ("ChIJDwx76JCDGjkRzcG5j5oovfo", "https://www.practo.com/ludhiana/clinic/ludhiana-orthodontic-and-dental-clinic-model-town"),
    ("ChIJa9gkQAyDGjkRlgSUwgTpR_o", "https://www.practo.com/ludhiana/clinic/dentology-by-dr-sagar-model-town"),
    ("ChIJDf1wnkyCGjkRy75NWE5KIBw", "https://www.practo.com/ludhiana/clinic/dr-garg-s-dental-care-model-town"),
    ("ChIJ8dJNFC6DGjkRJTTL7Ccr5r4", "https://www.practo.com/ludhiana/clinic/dr-rohit-dental-clinic-sarabha"),
]


def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set in .env")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    gp_repo = GooglePlacesLeadRepository(str(DB_PATH))
    practo_repo = PractoListingRepository(str(DB_PATH))
    pair_repo = MatchPairRepository(str(DB_PATH))

    judge = LLMJudge(client, pair_repo)

    confirmed = 0
    rejected = 0

    for gp_id, practo_url in OBVIOUS_PAIRS:
        gp_lead = gp_repo.get_by_id(gp_id)
        practo_listing = practo_repo.get_by_url(practo_url)

        if gp_lead is None:
            print(f"  MISSING GP  {gp_id}")
            continue
        if practo_listing is None:
            print(f"  MISSING Practo  {practo_url}")
            continue

        row_a: MatchableRow = from_google_places(gp_lead)
        row_b: MatchableRow = from_practo(practo_listing)
        geo_dist = _haversine_km(row_a.lat or 0, row_a.lng or 0, row_b.lat or 0, row_b.lng or 0)

        pair = CandidatePair(
            row_a=row_a,
            row_b=row_b,
            geo_distance_km=geo_dist,
            passed_geo=True,
            passed_name=True,
        )

        print(f"\n{'─'*70}")
        print(f"  GP:     {row_a.name}")
        print(f"  Practo: {row_b.name}")
        print(f"  Dist:   {geo_dist*1000:.0f}m")

        verdict = judge.evaluate_pair(pair)

        if verdict is not None:
            print(f"  CONFIRMED  confidence={verdict.confidence:.2f}")
            print(f"  Reviewer reason: {verdict.reason}")
            confirmed += 1
        else:
            # Read back proposer reason for context.
            proposer = pair_repo.get(
                row_a.source, row_a.key,
                row_b.source, row_b.key,
                "proposer",
            )
            if proposer:
                print(f"  REJECTED   proposer match={proposer.match} confidence={proposer.confidence:.2f}")
                print(f"  Proposer reason: {proposer.reason}")
            else:
                print("  REJECTED (proposer returned no result)")
            rejected += 1

    gp_repo.close()
    practo_repo.close()
    pair_repo.close()

    print(f"\n{'='*70}")
    print(f"Results: {confirmed} confirmed / {rejected} rejected out of {len(OBVIOUS_PAIRS)} pairs")


if __name__ == "__main__":
    main()
