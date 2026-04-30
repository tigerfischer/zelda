"""Tests for the EnrichLeadsPipeline orchestrator."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zelda.controllers.enrichment.pipeline import EnrichLeadsPipeline
from zelda.models.lead import Lead
from zelda.models.lead_enrichment import LeadEnrichment
from zelda.repositories.lead_enrichment_repo import LeadEnrichmentRepository
from zelda.repositories.lead_repo import LeadRepository


def _lead(lead_id: str = "lead-1") -> Lead:
    return Lead(
        lead_id=lead_id,
        city="Ludhiana",
        run_id="run-1",
        tier="standalone",
        name=f"Clinic {lead_id}",
        lybrate_urls=[],
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def lead_repo(db_path) -> LeadRepository:
    r = LeadRepository(db_path)
    yield r
    r.close()


@pytest.fixture
def enrichment_repo(db_path) -> LeadEnrichmentRepository:
    r = LeadEnrichmentRepository(db_path)
    yield r
    r.close()


def _make_pipeline(db_path, lead_repo, enrichment_repo, leads: list[Lead]) -> EnrichLeadsPipeline:
    lead_repo.insert_many(leads)

    gp_repo = MagicMock()
    gp_repo.get_by_id.return_value = None

    practo_repo = MagicMock()
    practo_repo.get_by_url.return_value = None

    lybrate_repo = MagicMock()
    lybrate_repo.get_by_url.return_value = None

    review_repo = MagicMock()
    review_repo.get_reviews_for_place.return_value = []

    website_gw = MagicMock()
    website_gw.audit.return_value = {
        "website_loads": True,
        "is_mobile_friendly": True,
        "has_schema_markup": False,
        "has_blog": False,
        "has_whatsapp_link": False,
        "has_online_booking": False,
        "has_chat_widget": False,
        "agency_credit": None,
        "page_text": "",
        "error": None,
    }

    return EnrichLeadsPipeline(
        db_path=db_path,
        lead_repo=lead_repo,
        enrichment_repo=enrichment_repo,
        gp_repo=gp_repo,
        practo_repo=practo_repo,
        lybrate_repo=lybrate_repo,
        review_repo=review_repo,
        website_gateway=website_gw,
        inter_lead_delay_s=0,
    )


class TestBasicRun:
    def test_runs_all_passes_by_default(self, db_path, lead_repo, enrichment_repo):
        leads = [_lead("lead-1"), _lead("lead-2")]
        pipeline = _make_pipeline(db_path, lead_repo, enrichment_repo, leads)

        result = pipeline.run("Ludhiana")

        assert result.n_leads == 2
        assert len(result.errors) == 0

        e1 = enrichment_repo.get("lead-1")
        assert e1 is not None
        assert "pass0" in e1.passes_completed
        assert "pass5" in e1.passes_completed

    def test_produces_score_and_tier(self, db_path, lead_repo, enrichment_repo):
        pipeline = _make_pipeline(db_path, lead_repo, enrichment_repo, [_lead()])

        pipeline.run("Ludhiana")

        e = enrichment_repo.get("lead-1")
        assert e.need_score is not None
        assert e.score_tier in ("hot", "warm", "cold", "disqualified")

    def test_no_leads_returns_empty_result(self, db_path, lead_repo, enrichment_repo):
        pipeline = _make_pipeline(db_path, lead_repo, enrichment_repo, [])
        result = pipeline.run("Ludhiana")
        assert result.n_leads == 0
        assert len(result.errors) == 0


class TestPassSelection:
    def test_only_pass0_runs(self, db_path, lead_repo, enrichment_repo):
        pipeline = _make_pipeline(db_path, lead_repo, enrichment_repo, [_lead()])

        pipeline.run("Ludhiana", passes={0})

        e = enrichment_repo.get("lead-1")
        assert "pass0" in e.passes_completed
        assert "pass1" not in e.passes_completed
        assert "pass5" not in e.passes_completed
        assert e.need_score is None  # scoring didn't run

    def test_only_scoring_runs(self, db_path, lead_repo, enrichment_repo):
        # Pre-populate enrichment
        pre = LeadEnrichment(lead_id="lead-1", city="Ludhiana")
        pre.google_review_count = 10
        enrichment_repo.upsert(pre)

        pipeline = _make_pipeline(db_path, lead_repo, enrichment_repo, [_lead()])
        pipeline.run("Ludhiana", passes={5})

        e = enrichment_repo.get("lead-1")
        assert "pass5" in e.passes_completed
        assert e.need_score is not None


class TestCaching:
    def test_cached_pass_not_re_run(self, db_path, lead_repo, enrichment_repo):
        # Mark pass0 as already done
        pre = LeadEnrichment(lead_id="lead-1", city="Ludhiana")
        pre.passes_completed = {"pass0": "2026-04-30T10:00:00+00:00"}
        pre.google_review_count = 99  # sentinel value
        enrichment_repo.upsert(pre)

        pipeline = _make_pipeline(db_path, lead_repo, enrichment_repo, [_lead()])
        pipeline._gp_repo.get_by_id.return_value = None  # would clear review_count if run

        pipeline.run("Ludhiana", passes={0})

        e = enrichment_repo.get("lead-1")
        # Should still have the old sentinel value — pass0 was skipped
        assert e.google_review_count == 99

    def test_force_reruns_cached_pass(self, db_path, lead_repo, enrichment_repo):
        pre = LeadEnrichment(lead_id="lead-1", city="Ludhiana")
        pre.passes_completed = {"pass0": "2026-04-30T10:00:00+00:00"}
        pre.google_review_count = 99
        enrichment_repo.upsert(pre)

        pipeline = _make_pipeline(db_path, lead_repo, enrichment_repo, [_lead()])
        pipeline.run("Ludhiana", passes={0}, force=True)

        e = enrichment_repo.get("lead-1")
        # Pass0 re-ran; gp_repo returned None → GP signals cleared to None
        assert e.google_review_count is None
        assert e.google_rating is None


class TestTierCounts:
    def test_tallies_disqualified(self, db_path, lead_repo, enrichment_repo):
        chain_lead = _lead("lead-chain")
        chain_lead = Lead(
            lead_id="lead-chain",
            city="Ludhiana",
            run_id="run-1",
            tier="standalone",
            name="Clove Dental Model Town",  # known chain
            lybrate_urls=[],
            created_at=datetime.now(timezone.utc),
        )
        pipeline = _make_pipeline(db_path, lead_repo, enrichment_repo, [chain_lead])
        result = pipeline.run("Ludhiana")
        assert result.n_disqualified == 1
