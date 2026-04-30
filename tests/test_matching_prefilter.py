"""Tests for the Stage 1 pre-filter (geo + name token)."""

from __future__ import annotations

from zelda.controllers.matching.prefilter import (
    build_candidate_pairs,
    name_tokens,
)
from zelda.models.matchable_row import MatchableRow


def _row(
    source: str,
    key: str,
    name: str,
    *,
    lat: float | None = None,
    lng: float | None = None,
) -> MatchableRow:
    return MatchableRow(
        source=source, key=key, name=name, city="Ludhiana",
        lat=lat, lng=lng,
    )


# Ludhiana coordinates used throughout
_GP_LAT, _GP_LNG = 30.9010, 75.8573


# ── name_tokens ─────────────────────────────────────────────────────

def test_name_tokens_strips_stopwords():
    tokens = name_tokens("Sharma Dental Clinic")
    assert "dental" not in tokens
    assert "clinic" not in tokens
    assert "sharma" in tokens


def test_name_tokens_strips_punctuation():
    tokens = name_tokens("Dr. Puri's Dental Care")
    assert "puri" in tokens
    assert "dr" not in tokens


def test_name_tokens_empty_after_stopwords():
    # All tokens are stopwords — result is empty frozenset
    tokens = name_tokens("Dental Clinic")
    assert tokens == frozenset()


def test_name_tokens_short_tokens_excluded():
    tokens = name_tokens("AB Dental")
    # "ab" is length 2 and not a stopword — included
    assert "ab" in tokens


# ── build_candidate_pairs ────────────────────────────────────────────

def test_geo_filter_includes_nearby_gp_practo_pair():
    gp = [_row("google_places", "p1", "Sharma Dental", lat=_GP_LAT, lng=_GP_LNG)]
    # 0.1 km away — well within 1 km
    practo = [_row("practo", "u1", "Arora Clinic", lat=_GP_LAT + 0.0005, lng=_GP_LNG)]

    pairs = build_candidate_pairs(gp, practo)
    assert len(pairs) == 1
    assert pairs[0].passed_geo is True


def test_geo_filter_excludes_far_gp_practo_pair():
    gp = [_row("google_places", "p1", "Sharma Dental", lat=_GP_LAT, lng=_GP_LNG)]
    # ~3.5 km away — beyond 1 km
    practo = [_row("practo", "u1", "Arora Clinic", lat=_GP_LAT + 0.03, lng=_GP_LNG)]

    pairs = build_candidate_pairs(gp, practo, geo_radius_km=1.0)
    # Name filter would catch if they shared a token — they don't here
    assert len(pairs) == 0


def test_name_filter_catches_shared_token_regardless_of_distance():
    gp = [_row("google_places", "p1", "Puri Dental Clinic", lat=_GP_LAT, lng=_GP_LNG)]
    # Very far, but same name token "puri"
    practo = [_row("practo", "u1", "Puri Clinic", lat=_GP_LAT + 0.5, lng=_GP_LNG)]

    pairs = build_candidate_pairs(gp, practo)
    assert len(pairs) == 1
    assert pairs[0].passed_name is True


def test_no_geo_filter_for_gp_lybrate_pair():
    """GP↔Lybrate must use name filter only, even if geo would pass."""
    gp = [_row("google_places", "p1", "Puri Dental", lat=_GP_LAT, lng=_GP_LNG)]
    # Same location, same name token
    lybrate = [_row("lybrate", "u1", "Dr Puri", lat=_GP_LAT, lng=_GP_LNG)]

    pairs = build_candidate_pairs(gp, lybrate)
    assert len(pairs) == 1
    assert pairs[0].passed_geo is False  # geo not applied for lybrate
    assert pairs[0].passed_name is True


def test_no_geo_filter_for_practo_lybrate_pair():
    practo = [_row("practo", "u1", "Sharma Dental", lat=_GP_LAT, lng=_GP_LNG)]
    lybrate = [_row("lybrate", "u2", "Dr Sharma", lat=_GP_LAT, lng=_GP_LNG)]

    pairs = build_candidate_pairs(practo, lybrate)
    assert pairs[0].passed_geo is False
    assert pairs[0].passed_name is True


def test_no_shared_token_and_far_apart_produces_no_pairs():
    gp = [_row("google_places", "p1", "Bright Smile Centre", lat=_GP_LAT, lng=_GP_LNG)]
    practo = [_row("practo", "u1", "Arora Hospital", lat=_GP_LAT + 0.05, lng=_GP_LNG)]

    pairs = build_candidate_pairs(gp, practo)
    assert pairs == []


def test_missing_geo_falls_through_to_name_for_gp_practo():
    gp = [_row("google_places", "p1", "Puri Dental")]  # no lat/lng
    practo = [_row("practo", "u1", "Puri Clinic", lat=_GP_LAT, lng=_GP_LNG)]

    pairs = build_candidate_pairs(gp, practo)
    # Geo not applicable (no coords), name filter catches it
    assert len(pairs) == 1
    assert pairs[0].passed_name is True
    assert pairs[0].geo_distance_km is None


def test_empty_source_list_returns_no_pairs():
    gp = [_row("google_places", "p1", "Puri Dental", lat=_GP_LAT, lng=_GP_LNG)]
    assert build_candidate_pairs(gp, []) == []
    assert build_candidate_pairs([], gp) == []


def test_candidate_pair_filter_reasons():
    gp = [_row("google_places", "p1", "Puri Dental", lat=_GP_LAT, lng=_GP_LNG)]
    practo = [_row("practo", "u1", "Puri Clinic", lat=_GP_LAT + 0.0001, lng=_GP_LNG)]

    pairs = build_candidate_pairs(gp, practo)
    reasons = pairs[0].filter_reasons
    assert "name_token" in reasons
    assert any(r.startswith("geo:") for r in reasons)


def test_multiple_rows_generates_multiple_pairs():
    gp = [
        _row("google_places", "p1", "Puri Dental", lat=_GP_LAT, lng=_GP_LNG),
        _row("google_places", "p2", "Sharma Dental", lat=_GP_LAT, lng=_GP_LNG),
    ]
    practo = [
        _row("practo", "u1", "Puri Clinic", lat=_GP_LAT + 0.001, lng=_GP_LNG),
        _row("practo", "u2", "Arora Clinic", lat=_GP_LAT + 0.001, lng=_GP_LNG),
    ]
    # p1↔u1 (geo + name), p1↔u2 (geo only), p2↔u1 (geo only), p2↔u2 (geo only)
    pairs = build_candidate_pairs(gp, practo)
    assert len(pairs) == 4
