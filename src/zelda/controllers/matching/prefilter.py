"""Stage 1 — pre-filter candidate pairs before LLM evaluation.

Rules (applied per source pair):
- GP ↔ Practo:  geo (≤ GEO_RADIUS_KM) OR name-token overlap
- GP ↔ Lybrate: name-token overlap ONLY (Lybrate coordinates may be
                home addresses, not clinic addresses)
- Practo ↔ Lybrate: name-token overlap ONLY (same reasoning)

A pair that passes either applicable filter becomes a candidate.
The LLM sees only these candidates — not the full cross-product.

Name-token matching
-------------------
Both names are lowercased, stripped of punctuation, and split on whitespace.
A shared stopword list removes ubiquitous dental terms that would otherwise
create false positives. At least one shared non-stopword token is required.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from zelda.models.matchable_row import MatchableRow


GEO_RADIUS_KM: float = 1.0

# Sources for which geo pre-filtering is safe (both are clinic-addressed).
_GEO_ELIGIBLE_PAIRS: frozenset[frozenset[str]] = frozenset({
    frozenset({"google_places", "practo"}),
})

_STOPWORDS: frozenset[str] = frozenset({
    "dental", "dentist", "dentists", "clinic", "clinics", "centre", "center",
    "care", "hospital", "teeth", "tooth", "smile", "oral", "dr", "doctor",
    "doctors", "and", "the", "a", "an", "of", "in", "at", "near", "city",
    "new", "best", "advanced", "modern", "family",
})


@dataclass(frozen=True)
class CandidatePair:
    row_a: MatchableRow
    row_b: MatchableRow
    geo_distance_km: float | None   # None when either row lacks coordinates
    passed_geo: bool
    passed_name: bool

    @property
    def filter_reasons(self) -> list[str]:
        reasons = []
        if self.passed_geo:
            reasons.append(f"geo:{self.geo_distance_km:.2f}km")
        if self.passed_name:
            reasons.append("name_token")
        return reasons


def build_candidate_pairs(
    rows_a: list[MatchableRow],
    rows_b: list[MatchableRow],
    *,
    geo_radius_km: float = GEO_RADIUS_KM,
) -> list[CandidatePair]:
    """Return all (a, b) pairs that pass the pre-filter.

    The pair (source_a, source_b) determines which filters apply.
    Caller controls the direction; this function is not symmetric —
    it generates pairs in the order (rows_a[i], rows_b[j]).
    """
    if not rows_a or not rows_b:
        return []

    source_a = rows_a[0].source
    source_b = rows_b[0].source
    use_geo = frozenset({source_a, source_b}) in _GEO_ELIGIBLE_PAIRS

    pairs: list[CandidatePair] = []
    for a in rows_a:
        for b in rows_b:
            dist = _haversine_km(a.lat, a.lng, b.lat, b.lng)
            geo_ok = use_geo and dist is not None and dist <= geo_radius_km
            name_ok = _names_share_token(a.name, b.name)
            if geo_ok or name_ok:
                pairs.append(CandidatePair(
                    row_a=a, row_b=b,
                    geo_distance_km=dist,
                    passed_geo=geo_ok,
                    passed_name=name_ok,
                ))
    return pairs


def name_tokens(name: str) -> frozenset[str]:
    # Replace punctuation with spaces so "Puri's" → "puri s" → {"puri"}
    cleaned = re.sub(r"[^a-z0-9\s]", " ", name.lower())
    return frozenset(t for t in cleaned.split() if t not in _STOPWORDS and len(t) > 1)


def _names_share_token(a: str, b: str) -> bool:
    return bool(name_tokens(a) & name_tokens(b))


def _haversine_km(
    lat1: float | None, lng1: float | None,
    lat2: float | None, lng2: float | None,
) -> float | None:
    if any(v is None for v in (lat1, lng1, lat2, lng2)):
        return None
    R = 6_371.0
    dlat = math.radians(lat2 - lat1)  # type: ignore[operator]
    dlng = math.radians(lng2 - lng1)  # type: ignore[operator]
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2  # type: ignore[arg-type]
    )
    return R * 2 * math.asin(math.sqrt(a))


__all__ = [
    "GEO_RADIUS_KM",
    "CandidatePair",
    "build_candidate_pairs",
    "name_tokens",
]
