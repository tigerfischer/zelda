"""Tests for LeadEnrichmentRepository."""

from datetime import datetime, timezone

import pytest

from zelda.models.lead_enrichment import LeadEnrichment
from zelda.repositories.lead_enrichment_repo import LeadEnrichmentRepository


@pytest.fixture
def repo(tmp_path):
    r = LeadEnrichmentRepository(tmp_path / "test.db")
    yield r
    r.close()


def _make(lead_id: str = "lead-1", city: str = "Ludhiana") -> LeadEnrichment:
    return LeadEnrichment(lead_id=lead_id, city=city)


class TestSchema:
    def test_creates_table(self, repo):
        count = repo.count_for_city("Ludhiana")
        assert count == 0


class TestGetOrCreate:
    def test_creates_fresh_record(self, repo):
        e = repo.get_or_create("lead-1", city="Ludhiana")
        assert e.lead_id == "lead-1"
        assert e.city == "Ludhiana"
        assert e.need_score is None

    def test_returns_existing(self, repo):
        e1 = repo.get_or_create("lead-1", city="Ludhiana")
        e1.need_score = 80
        repo.upsert(e1)
        e2 = repo.get_or_create("lead-1", city="Ludhiana")
        assert e2.need_score == 80

    def test_idempotent(self, repo):
        repo.get_or_create("lead-1", city="Ludhiana")
        repo.get_or_create("lead-1", city="Ludhiana")
        assert repo.count_for_city("Ludhiana") == 1


class TestUpsert:
    def test_stores_all_signal_types(self, repo):
        e = _make()
        e.google_review_count = 42
        e.google_rating = 4.3
        e.gbp_has_hours = True
        e.gbp_photos_count = 10
        e.gbp_has_description = False
        e.is_not_operational = False
        e.review_velocity_30d = 5
        e.owner_response_rate = 0.75
        e.has_revenue_leak_signal = True
        e.negative_theme_flags = ["no_reply", "wait_time"]
        e.has_website = True
        e.on_practo = True
        e.on_lybrate = False
        e.source_count = 2
        e.nap_consistent = True
        e.is_chain = False
        e.has_whatsapp_link = False
        e.has_online_booking = True
        e.service_mix = ["general", "implants"]
        e.equipment_claims = ["cbct"]
        e.owner_name = "Dr. Sharma"
        e.direct_phone = "9876543210"
        e.need_score = 72
        e.score_tier = "hot"
        e.pitch_angle = "reviews"
        e.passes_completed = {"pass0": "2026-04-30T10:00:00+00:00"}
        e.updated_at = datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc)
        repo.upsert(e)

        got = repo.get("lead-1")
        assert got is not None
        assert got.google_review_count == 42
        assert got.google_rating == 4.3
        assert got.gbp_has_hours is True
        assert got.gbp_has_description is False
        assert got.review_velocity_30d == 5
        assert got.owner_response_rate == 0.75
        assert got.has_revenue_leak_signal is True
        assert got.negative_theme_flags == ["no_reply", "wait_time"]
        assert got.service_mix == ["general", "implants"]
        assert got.equipment_claims == ["cbct"]
        assert got.owner_name == "Dr. Sharma"
        assert got.need_score == 72
        assert got.score_tier == "hot"
        assert got.pitch_angle == "reviews"
        assert "pass0" in got.passes_completed

    def test_overwrites_on_second_upsert(self, repo):
        e = _make()
        e.need_score = 50
        repo.upsert(e)
        e.need_score = 80
        repo.upsert(e)
        assert repo.get("lead-1").need_score == 80

    def test_none_fields_round_trip(self, repo):
        e = _make()
        repo.upsert(e)
        got = repo.get("lead-1")
        assert got.google_review_count is None
        assert got.has_whatsapp_link is None
        assert got.score_tier is None


class TestGetForCity:
    def test_returns_all_for_city(self, repo):
        for i in range(3):
            e = LeadEnrichment(lead_id=f"lead-{i}", city="Ludhiana")
            e.need_score = i * 10
            repo.upsert(e)
        results = repo.get_for_city("Ludhiana")
        assert len(results) == 3

    def test_filters_by_other_city(self, repo):
        repo.upsert(LeadEnrichment(lead_id="a", city="Ludhiana"))
        repo.upsert(LeadEnrichment(lead_id="b", city="Delhi"))
        assert len(repo.get_for_city("Ludhiana")) == 1
        assert len(repo.get_for_city("Delhi")) == 1

    def test_filters_by_tier(self, repo):
        hot = LeadEnrichment(lead_id="h", city="Ludhiana")
        hot.score_tier = "hot"
        hot.need_score = 80
        warm = LeadEnrichment(lead_id="w", city="Ludhiana")
        warm.score_tier = "warm"
        warm.need_score = 55
        repo.upsert(hot)
        repo.upsert(warm)
        assert len(repo.get_for_city("Ludhiana", tier="hot")) == 1
        assert len(repo.get_for_city("Ludhiana", tier="warm")) == 1

    def test_ordered_by_score_desc(self, repo):
        for score in [30, 80, 55]:
            e = LeadEnrichment(lead_id=f"lead-{score}", city="Ludhiana")
            e.need_score = score
            repo.upsert(e)
        results = repo.get_for_city("Ludhiana")
        scores = [r.need_score for r in results]
        assert scores == sorted(scores, reverse=True)


class TestCountWithPass:
    def test_counts_completed_pass(self, repo):
        e = _make()
        e.passes_completed = {"pass0": "2026-04-30T10:00:00+00:00"}
        repo.upsert(e)
        assert repo.count_with_pass("Ludhiana", "pass0") == 1
        assert repo.count_with_pass("Ludhiana", "pass1") == 0
