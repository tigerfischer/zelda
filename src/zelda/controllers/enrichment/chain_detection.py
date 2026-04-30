"""Known dental chain and hospital detection — no external dependencies.

Used by Pass 0 to flag leads that should be disqualified from Zelda's
ICP (independent 1-3 chair practices). Chains and hospital-embedded
departments don't benefit from Zelda's patient-acquisition features.
"""

from __future__ import annotations

# Prominent dental chains in India (partial names, matched case-insensitively)
KNOWN_CHAINS: frozenset[str] = frozenset({
    "clove dental", "apollo white dental", "apollo dental",
    "sabka dentist", "sabka dental", "confident dental",
    "makewell dental", "make well dental", "32 smile stone",
    "tooth town", "smile zone", "dentzz", "kool smiles",
    "tooth fairy", "ivory dental", "dr smile", "denta world",
    "oracare", "new age dental", "care dental", "mydentist",
    "the dental company", "bds dental",
})

HOSPITAL_KEYWORDS: frozenset[str] = frozenset({
    "hospital", "multi-specialty", "multispecialty", "nursing home",
    "medical centre", "medical center", "polyclinic", "health centre",
    "health center", "super specialty", "super speciality",
})


def detect_chain(name: str) -> bool:
    """Return True if `name` matches any known dental chain."""
    name_lower = name.lower()
    return any(chain in name_lower for chain in KNOWN_CHAINS)


def detect_hospital(name: str, address: str | None = None) -> bool:
    """Return True if the clinic appears to be hospital-embedded."""
    combined = (name + " " + (address or "")).lower()
    return any(kw in combined for kw in HOSPITAL_KEYWORDS)


__all__ = ["KNOWN_CHAINS", "HOSPITAL_KEYWORDS", "detect_chain", "detect_hospital"]
