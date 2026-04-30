"""Tests for enrichment Pass 5 — lead scoring."""

from datetime import datetime, timezone

import pytest

from zelda.controllers.enrichment import pass5_scoring
from zelda.models.lead import Lead
from zelda.models.lead_enrichment import LeadEnrichment


def _lead(name: str = "Test Dental Clinic") -> Lead:
    return Lead(
        lead_id="lead-1",
        city="Ludhiana",
        run_id="run-1",
        tier="standalone",
        name=name,
        lybrate_urls=[],
        created_at=datetime.now(timezone.utc),
    )


def _enrichment(**kwargs) -> LeadEnrichment:
    e = LeadEnrichment(lead_id="lead-1", city="Ludhiana")
    for k, v in kwargs.items():
        setattr(e, k, v)
    return e


class TestDisqualifiers:
    def test_chain_is_disqualified(self):
        e = _enrichment(is_chain=True)
        result = pass5_scoring.run(_lead(), e)
        assert result.score_tier == "disqualified"
        assert result.need_score == 0
        assert result.pitch_angle == "disqualified"

    def test_hospital_is_disqualified(self):
        e = _enrichment(is_hospital_embedded=True)
        result = pass5_scoring.run(_lead(), e)
        assert result.score_tier == "disqualified"

    def test_not_operational_is_disqualified(self):
        e = _enrichment(is_not_operational=True)
        result = pass5_scoring.run(_lead(), e)
        assert result.score_tier == "disqualified"


class TestReputationScore:
    def test_very_few_reviews_scores_high(self):
        # rep=70 (count<20→40 + v90=0→30), acq≈65 (unknown signals), conv=0, fit=0
        # weighted: 70*0.30 + 65*0.25 = 37
        e = _enrichment(google_review_count=5, review_velocity_90d=0)
        result = pass5_scoring.run(_lead(), e)
        assert result.need_score >= 30

    def test_many_reviews_lower_reputation_score(self):
        e1 = _enrichment(google_review_count=10, review_velocity_90d=0)
        e2 = _enrichment(google_review_count=200, review_velocity_90d=10)
        r1 = pass5_scoring.run(_lead(), e1)
        r2 = pass5_scoring.run(_lead(), e2)
        assert r1.need_score > r2.need_score

    def test_revenue_leak_boosts_score(self):
        e1 = _enrichment(google_review_count=50, has_revenue_leak_signal=True)
        e2 = _enrichment(google_review_count=50, has_revenue_leak_signal=False)
        r1 = pass5_scoring.run(_lead(), e1)
        r2 = pass5_scoring.run(_lead(), e2)
        assert r1.need_score > r2.need_score

    def test_zero_response_rate_adds_points(self):
        e1 = _enrichment(owner_response_rate=0.0)
        e2 = _enrichment(owner_response_rate=1.0)
        r1 = pass5_scoring.run(_lead(), e1)
        r2 = pass5_scoring.run(_lead(), e2)
        assert r1.need_score > r2.need_score


class TestAcquisitionScore:
    def test_no_website_scores_high(self):
        # rep=30 (unknown count), acq=100 (no website→50 + missing GBP→45 + single source→20)
        # weighted: 30*0.30 + 100*0.25 = 34
        e = _enrichment(has_website=False)
        result = pass5_scoring.run(_lead(), e)
        assert result.need_score >= 30

    def test_missing_gbp_hours_adds_points(self):
        e1 = _enrichment(gbp_has_hours=False)
        e2 = _enrichment(gbp_has_hours=True)
        r1 = pass5_scoring.run(_lead(), e1)
        r2 = pass5_scoring.run(_lead(), e2)
        assert r1.need_score > r2.need_score


class TestConversionScore:
    def test_no_whatsapp_adds_points(self):
        e1 = _enrichment(has_whatsapp_link=False)
        e2 = _enrichment(has_whatsapp_link=True)
        r1 = pass5_scoring.run(_lead(), e1)
        r2 = pass5_scoring.run(_lead(), e2)
        assert r1.need_score > r2.need_score


class TestTiers:
    def test_hot_tier(self):
        e = _enrichment(
            google_review_count=5,
            review_velocity_90d=0,
            has_revenue_leak_signal=True,
            has_website=False,
            has_whatsapp_link=False,
            has_online_booking=False,
            on_practo=True,
            direct_phone="9876543210",
        )
        result = pass5_scoring.run(_lead(), e)
        assert result.score_tier == "hot"

    def test_cold_tier(self):
        e = _enrichment(
            google_review_count=200,
            review_velocity_90d=15,
            owner_response_rate=0.9,
            has_revenue_leak_signal=False,
            has_website=True,
            website_loads=True,
            has_whatsapp_link=True,
            has_online_booking=True,
            gbp_has_hours=True,
            gbp_photos_count=20,
            gbp_has_description=True,
        )
        result = pass5_scoring.run(_lead(), e)
        assert result.score_tier == "cold"

    def test_score_clamped_0_100(self):
        # Worst possible clinic (everything bad)
        e = _enrichment(
            google_review_count=0,
            review_velocity_90d=0,
            owner_response_rate=0.0,
            has_revenue_leak_signal=True,
            has_website=False,
            gbp_has_hours=False,
            gbp_photos_count=0,
            has_whatsapp_link=False,
            has_online_booking=False,
        )
        result = pass5_scoring.run(_lead(), e)
        assert 0 <= result.need_score <= 100

    def test_score_is_integer(self):
        e = _enrichment(google_review_count=45)
        result = pass5_scoring.run(_lead(), e)
        assert isinstance(result.need_score, int)


class TestPitchAngle:
    def test_reviews_pitch_for_low_count(self):
        e = _enrichment(
            google_review_count=3,
            review_velocity_90d=0,
            has_revenue_leak_signal=True,
            has_website=True,
            website_loads=True,
            has_whatsapp_link=True,
        )
        result = pass5_scoring.run(_lead(), e)
        assert result.pitch_angle == "reviews"

    def test_gbp_pitch_for_missing_profile(self):
        e = _enrichment(
            google_review_count=100,
            review_velocity_90d=10,
            has_website=False,
            gbp_has_hours=False,
            gbp_photos_count=0,
        )
        result = pass5_scoring.run(_lead(), e)
        # Should be gbp or reviews — acquisition weakness dominates
        assert result.pitch_angle in ("gbp", "reviews")

    def test_pass_recorded(self):
        result = pass5_scoring.run(_lead(), _enrichment())
        assert "pass5" in result.passes_completed
