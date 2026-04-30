"""Tests for MatchPairRepository and LeadRepository."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from zelda.models.lead import Lead
from zelda.models.match_pair import MatchPairEvaluation
from zelda.repositories.lead_repo import LeadRepository
from zelda.repositories.match_pair_repo import MatchPairRepository


_T = datetime(2026, 4, 30, tzinfo=timezone.utc)


def _eval(
    source_a: str, key_a: str,
    source_b: str, key_b: str,
    stage: str = "proposer",
    match: bool = True,
    confidence: float | None = 0.9,
) -> MatchPairEvaluation:
    return MatchPairEvaluation(
        source_a=source_a, key_a=key_a,
        source_b=source_b, key_b=key_b,
        stage=stage,
        match=match,
        confidence=confidence,
        reason="test reason",
        model="claude-haiku-4-5-20251001",
        evaluated_at=_T,
    )


def _lead(
    lead_id: str = "l1",
    city: str = "Ludhiana",
    tier: str = "enriched",
    name: str = "Puri Dental",
) -> Lead:
    return Lead(
        lead_id=lead_id,
        city=city,
        run_id="match-test",
        tier=tier,
        name=name,
        created_at=_T,
    )


# ── MatchPairRepository ──────────────────────────────────────────────

@pytest.fixture
def pair_repo():
    r = MatchPairRepository(":memory:")
    yield r
    r.close()


def test_pair_repo_save_and_get(pair_repo):
    ev = _eval("google_places", "gp1", "practo", "pr1")
    pair_repo.save(ev)

    got = pair_repo.get("google_places", "gp1", "practo", "pr1", "proposer")
    assert got is not None
    assert got.match is True
    assert got.confidence == pytest.approx(0.9)


def test_pair_repo_canonical_ordering(pair_repo):
    """Saving (practo, p1, google_places, gp1) should be retrievable as (google_places, gp1, practo, p1)."""
    ev = _eval("practo", "pr1", "google_places", "gp1")
    pair_repo.save(ev)

    # Retrieve in the canonical direction
    got = pair_repo.get("google_places", "gp1", "practo", "pr1", "proposer")
    assert got is not None


def test_pair_repo_get_returns_none_when_missing(pair_repo):
    assert pair_repo.get("google_places", "gp1", "practo", "pr1", "proposer") is None


def test_pair_repo_upsert_overwrites(pair_repo):
    ev1 = _eval("google_places", "gp1", "practo", "pr1", match=True)
    ev2 = _eval("google_places", "gp1", "practo", "pr1", match=False)
    pair_repo.save(ev1)
    pair_repo.save(ev2)

    got = pair_repo.get("google_places", "gp1", "practo", "pr1", "proposer")
    assert got is not None
    assert got.match is False


def test_pair_repo_get_confirmed_matches(pair_repo):
    # Proposer + Reviewer both match for pair 1
    pair_repo.save(_eval("google_places", "gp1", "practo", "pr1", stage="proposer", match=True))
    pair_repo.save(_eval("google_places", "gp1", "practo", "pr1", stage="reviewer", match=True))
    # Proposer match but Reviewer rejects for pair 2
    pair_repo.save(_eval("google_places", "gp2", "practo", "pr2", stage="proposer", match=True))
    pair_repo.save(_eval("google_places", "gp2", "practo", "pr2", stage="reviewer", match=False))

    confirmed = pair_repo.get_confirmed_matches()
    assert len(confirmed) == 1
    assert confirmed[0].key_a == "gp1" or confirmed[0].key_b == "gp1"


def test_pair_repo_count_by_stage(pair_repo):
    pair_repo.save(_eval("google_places", "gp1", "practo", "pr1", stage="proposer"))
    pair_repo.save(_eval("google_places", "gp1", "practo", "pr1", stage="reviewer"))
    pair_repo.save(_eval("google_places", "gp2", "practo", "pr2", stage="proposer"))

    counts = pair_repo.count_by_stage()
    assert counts["proposer"] == 2
    assert counts["reviewer"] == 1


# ── LeadRepository ───────────────────────────────────────────────────

@pytest.fixture
def lead_repo():
    r = LeadRepository(":memory:")
    yield r
    r.close()


def test_lead_repo_insert_and_retrieve(lead_repo):
    lead_repo.insert_many([_lead("l1")])
    leads = lead_repo.get_for_city("Ludhiana")
    assert len(leads) == 1
    assert leads[0].name == "Puri Dental"


def test_lead_repo_empty_city_returns_empty(lead_repo):
    assert lead_repo.get_for_city("Bengaluru") == []


def test_lead_repo_count(lead_repo):
    lead_repo.insert_many([_lead("l1"), _lead("l2")])
    assert lead_repo.count_for_city("Ludhiana") == 2


def test_lead_repo_filter_by_tier(lead_repo):
    lead_repo.insert_many([
        _lead("l1", tier="enriched"),
        _lead("l2", tier="standalone"),
        _lead("l3", tier="enriched"),
    ])
    enriched = lead_repo.get_for_city("Ludhiana", tier="enriched")
    assert len(enriched) == 2
    assert all(l.tier == "enriched" for l in enriched)


def test_lead_repo_city_isolation(lead_repo):
    lead_repo.insert_many([
        _lead("l1", city="Ludhiana"),
        _lead("l2", city="Mumbai"),
    ])
    assert lead_repo.count_for_city("Ludhiana") == 1
    assert lead_repo.count_for_city("Mumbai") == 1


def test_lead_repo_insert_ignore_duplicate(lead_repo):
    lead_repo.insert_many([_lead("l1")])
    lead_repo.insert_many([_lead("l1")])  # same lead_id
    assert lead_repo.count_for_city("Ludhiana") == 1


def test_lead_repo_lybrate_urls_roundtrip(lead_repo):
    lead = Lead(
        lead_id="l1", city="Ludhiana", run_id="r1",
        tier="standalone", name="Dr Puri",
        lybrate_urls=["https://lybrate.com/ludhiana/dr-puri-1",
                      "https://lybrate.com/ludhiana/dr-puri-2"],
        created_at=_T,
    )
    lead_repo.insert_many([lead])
    got = lead_repo.get_for_city("Ludhiana")[0]
    assert len(got.lybrate_urls) == 2


def test_lead_repo_list_run_ids(lead_repo):
    lead_repo.insert_many([_lead("l1")])
    run_ids = lead_repo.list_run_ids("Ludhiana")
    assert run_ids == ["match-test"]
