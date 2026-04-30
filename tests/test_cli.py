import argparse

import pytest

from zelda.cli import (
    _max_results_type,
    _positive_int,
    build_parser,
)


# ── parser shape ────────────────────────────────────────────────────────


def test_parser_requires_subcommand():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_parser_rejects_unknown_subcommand():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["bogus", "--city", "Ludhiana"])


# ── discover subcommand ────────────────────────────────────────────────


def test_discover_basic_args():
    parser = build_parser()
    args = parser.parse_args(["discover", "--city", "Ludhiana"])

    assert args.command == "discover"
    assert args.city == "Ludhiana"
    assert args.max_results == 1  # default
    assert args.max_pages == 1     # default


def test_discover_requires_city():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["discover"])


def test_discover_max_results_int():
    parser = build_parser()
    args = parser.parse_args(["discover", "--city", "Ludhiana", "--max-results", "5"])
    assert args.max_results == 5


def test_discover_max_results_all_means_unlimited():
    parser = build_parser()
    args = parser.parse_args(["discover", "--city", "Ludhiana", "--max-results", "all"])
    assert args.max_results is None


def test_discover_max_results_zero_for_dry_run():
    parser = build_parser()
    args = parser.parse_args(["discover", "--city", "Ludhiana", "--max-results", "0"])
    assert args.max_results == 0


def test_discover_max_results_rejects_negative():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["discover", "--city", "Ludhiana", "--max-results", "-1"])


def test_discover_max_results_rejects_garbage():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["discover", "--city", "Ludhiana", "--max-results", "lots"])


def test_discover_max_pages_default_one():
    parser = build_parser()
    args = parser.parse_args(["discover", "--city", "Ludhiana"])
    assert args.max_pages == 1


def test_discover_max_pages_int():
    parser = build_parser()
    args = parser.parse_args(["discover", "--city", "Ludhiana", "--max-pages", "3"])
    assert args.max_pages == 3


def test_discover_max_pages_rejects_zero():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["discover", "--city", "Ludhiana", "--max-pages", "0"])


# ── sync subcommand ───────────────────────────────────────────────────


def test_sync_basic_args():
    parser = build_parser()
    args = parser.parse_args(["sync", "--city", "Ludhiana"])
    assert args.command == "sync"
    assert args.city == "Ludhiana"


def test_sync_requires_city():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["sync"])


# ── bootstrap subcommand ──────────────────────────────────────────────


def test_bootstrap_basic_args():
    parser = build_parser()
    args = parser.parse_args(["bootstrap", "--city", "Ludhiana"])
    assert args.command == "bootstrap"
    assert args.city == "Ludhiana"


def test_bootstrap_requires_city():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["bootstrap"])


# ── fetch-reviews subcommand ──────────────────────────────────────────


def test_fetch_reviews_basic_args():
    parser = build_parser()
    args = parser.parse_args(["fetch-reviews", "--city", "Ludhiana"])
    assert args.command == "fetch-reviews"
    assert args.city == "Ludhiana"
    # Defaults
    assert args.max_places == 1
    assert args.max_reviews_per_place == 100
    assert args.refresh_min_age_days == 7.0
    assert args.force_refresh is False
    assert args.headful is False


def test_fetch_reviews_requires_city():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["fetch-reviews"])


def test_fetch_reviews_max_places_int():
    parser = build_parser()
    args = parser.parse_args(
        ["fetch-reviews", "--city", "Ludhiana", "--max-places", "10"]
    )
    assert args.max_places == 10


def test_fetch_reviews_max_places_all_means_unlimited():
    parser = build_parser()
    args = parser.parse_args(
        ["fetch-reviews", "--city", "Ludhiana", "--max-places", "all"]
    )
    assert args.max_places is None


def test_fetch_reviews_max_places_zero_for_dry_run():
    parser = build_parser()
    args = parser.parse_args(
        ["fetch-reviews", "--city", "Ludhiana", "--max-places", "0"]
    )
    assert args.max_places == 0


def test_fetch_reviews_max_places_rejects_negative():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["fetch-reviews", "--city", "Ludhiana", "--max-places", "-1"]
        )


def test_fetch_reviews_max_reviews_per_place_int():
    parser = build_parser()
    args = parser.parse_args(
        ["fetch-reviews", "--city", "Ludhiana", "--max-reviews-per-place", "1000"]
    )
    assert args.max_reviews_per_place == 1000


def test_fetch_reviews_max_reviews_per_place_rejects_zero():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["fetch-reviews", "--city", "Ludhiana", "--max-reviews-per-place", "0"]
        )


def test_fetch_reviews_refresh_min_age_days_float():
    parser = build_parser()
    args = parser.parse_args(
        ["fetch-reviews", "--city", "Ludhiana", "--refresh-min-age-days", "3.5"]
    )
    assert args.refresh_min_age_days == 3.5


def test_fetch_reviews_refresh_min_age_days_zero_allowed():
    """Zero is a valid recency window — same effect as --force-refresh."""
    parser = build_parser()
    args = parser.parse_args(
        ["fetch-reviews", "--city", "Ludhiana", "--refresh-min-age-days", "0"]
    )
    assert args.refresh_min_age_days == 0.0


def test_fetch_reviews_refresh_min_age_days_rejects_negative():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["fetch-reviews", "--city", "Ludhiana", "--refresh-min-age-days", "-1"]
        )


def test_fetch_reviews_force_refresh_flag():
    parser = build_parser()
    args = parser.parse_args(
        ["fetch-reviews", "--city", "Ludhiana", "--force-refresh"]
    )
    assert args.force_refresh is True


def test_fetch_reviews_headful_flag():
    parser = build_parser()
    args = parser.parse_args(
        ["fetch-reviews", "--city", "Ludhiana", "--headful"]
    )
    assert args.headful is True


# ── enrich subcommand ────────────────────────────────────────────────


def test_enrich_basic_args():
    parser = build_parser()
    args = parser.parse_args(["enrich", "--city", "Ludhiana"])
    assert args.command == "enrich"
    assert args.city == "Ludhiana"
    # Defaults
    assert args.max_leads == 1
    assert args.max_age_days == 180.0
    assert args.force_refresh is False
    assert args.max_reviews_per_place == 1000
    assert args.sources is None
    assert args.headful is False


def test_enrich_requires_city():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["enrich"])


def test_enrich_max_leads_int():
    parser = build_parser()
    args = parser.parse_args(["enrich", "--city", "Ludhiana", "--max-leads", "10"])
    assert args.max_leads == 10


def test_enrich_max_leads_all_means_unlimited():
    parser = build_parser()
    args = parser.parse_args(["enrich", "--city", "Ludhiana", "--max-leads", "all"])
    assert args.max_leads is None


def test_enrich_max_leads_zero_for_dry_run():
    parser = build_parser()
    args = parser.parse_args(["enrich", "--city", "Ludhiana", "--max-leads", "0"])
    assert args.max_leads == 0


def test_enrich_max_leads_rejects_negative():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["enrich", "--city", "Ludhiana", "--max-leads", "-1"])


def test_enrich_max_age_days_float():
    parser = build_parser()
    args = parser.parse_args(["enrich", "--city", "Ludhiana", "--max-age-days", "30.5"])
    assert args.max_age_days == 30.5


def test_enrich_max_age_days_zero_allowed():
    """0 is valid — same as --force-refresh in effect."""
    parser = build_parser()
    args = parser.parse_args(["enrich", "--city", "Ludhiana", "--max-age-days", "0"])
    assert args.max_age_days == 0.0


def test_enrich_max_age_days_rejects_negative():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["enrich", "--city", "Ludhiana", "--max-age-days", "-1"])


def test_enrich_force_refresh_flag():
    parser = build_parser()
    args = parser.parse_args(["enrich", "--city", "Ludhiana", "--force-refresh"])
    assert args.force_refresh is True


def test_enrich_max_reviews_per_place_int():
    parser = build_parser()
    args = parser.parse_args(
        ["enrich", "--city", "Ludhiana", "--max-reviews-per-place", "500"]
    )
    assert args.max_reviews_per_place == 500


def test_enrich_max_reviews_per_place_rejects_zero():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["enrich", "--city", "Ludhiana", "--max-reviews-per-place", "0"]
        )


def test_enrich_sources_subset():
    parser = build_parser()
    args = parser.parse_args(
        ["enrich", "--city", "Ludhiana", "--sources", "google_reviews"]
    )
    assert args.sources == "google_reviews"


def test_enrich_sources_multiple():
    parser = build_parser()
    args = parser.parse_args(
        [
            "enrich", "--city", "Ludhiana",
            "--sources", "google_reviews,practo_profile",
        ]
    )
    assert args.sources == "google_reviews,practo_profile"


def test_enrich_headful_flag():
    parser = build_parser()
    args = parser.parse_args(["enrich", "--city", "Ludhiana", "--headful"])
    assert args.headful is True


# ── discover-practo-urls subcommand ──────────────────────────────────


def test_discover_practo_urls_basic_args():
    parser = build_parser()
    args = parser.parse_args(["discover-practo-urls", "--city", "Ludhiana"])
    assert args.command == "discover-practo-urls"
    assert args.city == "Ludhiana"
    # Defaults
    assert args.max_leads is None  # default 'all' for this command
    assert args.threshold == 0.7
    assert args.max_candidates == 10
    assert args.dry_run is False


def test_discover_practo_urls_requires_city():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["discover-practo-urls"])


def test_discover_practo_urls_max_leads_int():
    parser = build_parser()
    args = parser.parse_args(
        ["discover-practo-urls", "--city", "Ludhiana", "--max-leads", "5"]
    )
    assert args.max_leads == 5


def test_discover_practo_urls_max_leads_all_means_unlimited():
    parser = build_parser()
    args = parser.parse_args(
        ["discover-practo-urls", "--city", "Ludhiana", "--max-leads", "all"]
    )
    assert args.max_leads is None


def test_discover_practo_urls_threshold_in_range():
    parser = build_parser()
    args = parser.parse_args(
        ["discover-practo-urls", "--city", "Ludhiana", "--threshold", "0.85"]
    )
    assert args.threshold == 0.85


def test_discover_practo_urls_threshold_zero_allowed():
    parser = build_parser()
    args = parser.parse_args(
        ["discover-practo-urls", "--city", "Ludhiana", "--threshold", "0"]
    )
    assert args.threshold == 0.0


def test_discover_practo_urls_threshold_one_allowed():
    parser = build_parser()
    args = parser.parse_args(
        ["discover-practo-urls", "--city", "Ludhiana", "--threshold", "1"]
    )
    assert args.threshold == 1.0


def test_discover_practo_urls_threshold_rejects_above_one():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["discover-practo-urls", "--city", "Ludhiana", "--threshold", "1.5"]
        )


def test_discover_practo_urls_threshold_rejects_negative():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["discover-practo-urls", "--city", "Ludhiana", "--threshold", "-0.1"]
        )


def test_discover_practo_urls_max_candidates_int():
    parser = build_parser()
    args = parser.parse_args(
        ["discover-practo-urls", "--city", "Ludhiana", "--max-candidates", "20"]
    )
    assert args.max_candidates == 20


def test_discover_practo_urls_max_candidates_rejects_zero():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["discover-practo-urls", "--city", "Ludhiana", "--max-candidates", "0"]
        )


def test_discover_practo_urls_dry_run_flag():
    parser = build_parser()
    args = parser.parse_args(
        ["discover-practo-urls", "--city", "Ludhiana", "--dry-run"]
    )
    assert args.dry_run is True


# ── _unit_float type tests ───────────────────────────────────────────


def test_unit_float_accepts_zero():
    from zelda.cli import _unit_float
    assert _unit_float("0") == 0.0


def test_unit_float_accepts_one():
    from zelda.cli import _unit_float
    assert _unit_float("1") == 1.0


def test_unit_float_accepts_decimal():
    from zelda.cli import _unit_float
    assert _unit_float("0.5") == 0.5


def test_unit_float_rejects_above_one():
    from zelda.cli import _unit_float
    with pytest.raises(argparse.ArgumentTypeError):
        _unit_float("1.5")


def test_unit_float_rejects_negative():
    from zelda.cli import _unit_float
    with pytest.raises(argparse.ArgumentTypeError):
        _unit_float("-0.1")


def test_unit_float_rejects_garbage():
    from zelda.cli import _unit_float
    with pytest.raises(argparse.ArgumentTypeError):
        _unit_float("xyz")


# ── argument-type helpers ─────────────────────────────────────────────


def test_max_results_type_accepts_all():
    assert _max_results_type("all") is None
    assert _max_results_type("ALL") is None
    assert _max_results_type("All") is None


def test_max_results_type_accepts_zero():
    assert _max_results_type("0") == 0


def test_max_results_type_accepts_positive_int():
    assert _max_results_type("42") == 42


def test_max_results_type_rejects_negative():
    with pytest.raises(argparse.ArgumentTypeError):
        _max_results_type("-1")


def test_max_results_type_rejects_float():
    with pytest.raises(argparse.ArgumentTypeError):
        _max_results_type("3.14")


def test_max_results_type_rejects_garbage():
    with pytest.raises(argparse.ArgumentTypeError):
        _max_results_type("lots")


def test_positive_int_accepts_one_or_more():
    assert _positive_int("1") == 1
    assert _positive_int("100") == 100


def test_positive_int_rejects_zero():
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_int("0")


def test_positive_int_rejects_negative():
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_int("-5")


def test_positive_int_rejects_garbage():
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_int("xyz")
