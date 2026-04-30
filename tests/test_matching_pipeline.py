"""Integration tests for the MatchingPipeline with mocked LLM calls.

Verifies the full pipeline flow: load rows → pre-filter → LLM judge →
graph → synthesis → persist leads. The Anthropic client is mocked so
no real API calls are made.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from zelda.controllers.matching.pipeline import MatchingPipeline
from zelda.models.google_places_lead import GooglePlacesLead
from zelda.models.lybrate_listing import LybrateListing
from zelda.models.practo_listing import PractoListing
from zelda.repositories.google_places_lead_repo import GooglePlacesLeadRepository
from zelda.repositories.lead_repo import LeadRepository
from zelda.repositories.lybrate_listing_repo import LybrateListingRepository
from zelda.repositories.match_pair_repo import MatchPairRepository
from zelda.repositories.practo_listing_repo import PractoListingRepository


_T = datetime(2026, 4, 30, tzinfo=timezone.utc)
_LAT, _LNG = 30.9010, 75.8573


def _gp(place_id: str, name: str, lat=_LAT, lng=_LNG) -> GooglePlacesLead:
    return GooglePlacesLead(
        place_id=place_id, city="Ludhiana", name=name,
        lat=lat, lng=lng,
        discovered_at=_T, last_modified_at=_T,
    )


def _practo(profile_url: str, name: str, lat=_LAT, lng=_LNG) -> PractoListing:
    return PractoListing(
        profile_url=profile_url, city="Ludhiana", name=name,
        lat=lat, lng=lng,
        discovered_at=_T, last_modified_at=_T,
    )


def _lybrate(profile_url: str, doctor_name: str, lat=_LAT, lng=_LNG) -> LybrateListing:
    return LybrateListing(
        profile_url=profile_url, city="Ludhiana", doctor_name=doctor_name,
        lat=lat, lng=lng,
        discovered_at=_T, last_modified_at=_T,
    )


def _fake_response(tool_input: dict[str, Any]) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.input = tool_input
    msg = MagicMock()
    msg.content = [block]
    return msg


@pytest.fixture
def repos():
    gp = GooglePlacesLeadRepository(":memory:")
    practo = PractoListingRepository(":memory:")
    lybrate = LybrateListingRepository(":memory:")
    lead = LeadRepository(":memory:")
    pair = MatchPairRepository(":memory:")
    yield gp, practo, lybrate, lead, pair
    gp.close(); practo.close(); lybrate.close(); lead.close(); pair.close()


def _make_pipeline(repos, mock_client) -> MatchingPipeline:
    gp, practo, lybrate, lead, pair = repos
    return MatchingPipeline(
        gp_repo=gp, practo_repo=practo, lybrate_repo=lybrate,
        lead_repo=lead, pair_repo=pair,
        anthropic_client=mock_client,
    )


# ── empty city ───────────────────────────────────────────────────────

def test_empty_city_returns_zero_leads(repos):
    mock_client = MagicMock()
    pipeline = _make_pipeline(repos, mock_client)
    result = pipeline.run("Ludhiana")
    assert result.total_leads == 0
    assert mock_client.messages.create.call_count == 0


# ── all standalone (no matches) ──────────────────────────────────────

def test_no_matching_rows_all_become_standalone(repos):
    gp, practo, lybrate, lead, pair = repos
    gp.upsert_many([_gp("gp1", "Bright Smile")])
    practo.upsert_many([_practo("https://practo.com/c1", "Arora Hospital")])

    # Proposer rejects — no shared name token, different area
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(
        {"match": False, "confidence": 0.95, "reason": "different clinics"}
    )
    pipeline = _make_pipeline(repos, mock_client)
    result = pipeline.run("Ludhiana")

    assert result.standalone_leads == 2
    assert result.enriched_leads == 0
    assert result.reviewer_confirmed == 0

    leads = lead.get_for_city("Ludhiana")
    assert len(leads) == 2
    assert all(l.tier == "standalone" for l in leads)


# ── one confirmed match ───────────────────────────────────────────────

def test_confirmed_match_produces_enriched_lead(repos):
    gp, practo, lybrate, lead, pair = repos
    gp.upsert_many([_gp("gp1", "Puri Dental Clinic")])
    practo.upsert_many([_practo("https://practo.com/puri", "Puri Dental")])

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        # Proposer: match
        _fake_response({"match": True, "confidence": 0.9, "reason": "same name + area"}),
        # Reviewer: agree
        _fake_response({"agree": True, "reason": "confirmed — same address block"}),
        # Synthesis
        _fake_response({"name": "Puri Dental Clinic", "address": "Model Town", "notes": ""}),
    ]
    pipeline = _make_pipeline(repos, mock_client)
    result = pipeline.run("Ludhiana")

    assert result.enriched_leads == 1
    assert result.standalone_leads == 0
    assert result.reviewer_confirmed == 1

    leads = lead.get_for_city("Ludhiana")
    assert len(leads) == 1
    assert leads[0].tier == "enriched"
    assert leads[0].google_places_id == "gp1"
    assert leads[0].practo_url == "https://practo.com/puri"


# ── lybrate N-to-1 ───────────────────────────────────────────────────

def test_multiple_lybrate_rows_match_one_gp_clinic(repos):
    gp, practo, lybrate, lead, pair = repos
    gp.upsert_many([_gp("gp1", "Puri Dental Clinic")])
    lybrate.upsert_many([
        _lybrate("https://lybrate.com/dr-puri-1", "Dr Puri"),
        _lybrate("https://lybrate.com/dr-puri-2", "Dr Puri Senior"),
    ])

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        # GP↔Lybrate pair 1: proposer yes, reviewer yes
        _fake_response({"match": True, "confidence": 0.85, "reason": "same clinic, dr puri"}),
        _fake_response({"agree": True, "reason": "confirmed"}),
        # GP↔Lybrate pair 2: proposer yes, reviewer yes
        _fake_response({"match": True, "confidence": 0.82, "reason": "same clinic"}),
        _fake_response({"agree": True, "reason": "confirmed"}),
        # Synthesis for the 3-node cluster
        _fake_response({"name": "Puri Dental Clinic", "notes": "2 dentists"}),
    ]
    pipeline = _make_pipeline(repos, mock_client)
    result = pipeline.run("Ludhiana")

    assert result.enriched_leads == 1
    assert result.conflicts_flagged == 0  # N-to-1 Lybrate is not a conflict

    leads = lead.get_for_city("Ludhiana")
    assert len(leads) == 1
    enriched = leads[0]
    assert enriched.tier == "enriched"
    assert len(enriched.lybrate_urls) == 2


# ── run_id propagation ───────────────────────────────────────────────

def test_run_id_propagates_to_leads(repos):
    gp, practo, lybrate, lead, pair = repos
    gp.upsert_many([_gp("gp1", "Puri Dental")])

    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(
        {"match": False, "confidence": 0.9, "reason": "no match"}
    )
    pipeline = _make_pipeline(repos, mock_client)
    result = pipeline.run("Ludhiana", run_id="test-run-001")

    leads = lead.get_for_city("Ludhiana", run_id="test-run-001")
    assert len(leads) == 1
    assert leads[0].run_id == "test-run-001"
    assert result.run_id == "test-run-001"


# ── invalid city ─────────────────────────────────────────────────────

def test_empty_city_raises(repos):
    pipeline = _make_pipeline(repos, MagicMock())
    with pytest.raises(ValueError, match="city"):
        pipeline.run("")
    with pytest.raises(ValueError):
        pipeline.run("   ")
