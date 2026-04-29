"""Manual smoke test against the real Google Places API.

Run from project root:

    conda run -n zelda python scripts/smoke_places.py

What it does
------------
1. Loads Settings from .env.
2. Runs Text Search "dentist in Ludhiana" with max_pages=1 (~20 places).
3. Prints up to 5 results.
4. Fetches Place Details for the first result.
5. Prints a summary and saves the full response JSON to
   tests/fixtures/place_details_real_ludhiana.json (for use as a future
   integration-test fixture).

Cost: ~$0.06 per run (1 Text Search + 1 Place Details).

This is intentionally NOT a pytest test — it's a manual sanity check
that hits the live API. Unit tests (test_google_places_gateway.py)
cover the gateway logic with mocked HTTP and run offline.
"""

import json
from pathlib import Path

from zelda.config import Settings
from zelda.gateways.google_places import GooglePlacesGateway


def main() -> None:
    settings = Settings()
    print(f"Loaded Settings. API key prefix: {settings.google_places_api_key[:8]}…")

    with GooglePlacesGateway(api_key=settings.google_places_api_key) as gateway:
        print("\n--- Text Search: 'dentist in Ludhiana' (1 page) ---")
        places = gateway.text_search("dentist in Ludhiana", max_pages=1)
        print(f"Got {len(places)} place(s).\n")
        for p in places[:5]:
            print(f"  {p.id}")
            print(f"    {p.display_name.text}")
            print(f"    {p.formatted_address}")
            print()

        if not places:
            print(
                "No places returned. Check (a) the API key is set in .env, "
                "(b) 'Places API (New)' is enabled in the GCP project, and "
                "(c) billing is enabled on that project."
            )
            return

        first = places[0]
        print(f"--- Place Details for {first.id} ---")
        details = gateway.get_place_details(first.id)
        print(f"Name:       {details.get('displayName', {}).get('text')}")
        print(f"Address:    {details.get('formattedAddress')}")
        print(f"Phone (n):  {details.get('nationalPhoneNumber')}")
        print(f"Phone (i):  {details.get('internationalPhoneNumber')}")
        print(f"Website:    {details.get('websiteUri')}")
        print(f"Rating:     {details.get('rating')}  Reviews: {details.get('userRatingCount')}")
        print(f"Status:     {details.get('businessStatus')}")
        print(f"Type:       {details.get('primaryType')}")
        print(f"Reviews?    {bool(details.get('reviews'))}")
        print(f"Editorial?  {bool(details.get('editorialSummary'))}")
        print(f"Photos:     {len(details.get('photos') or [])}")

        out = (
            Path(__file__).resolve().parent.parent
            / "tests"
            / "fixtures"
            / "place_details_real_ludhiana.json"
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(details, indent=2, ensure_ascii=False))
        print(f"\nSaved real Place Details response to: {out}")


if __name__ == "__main__":
    main()
