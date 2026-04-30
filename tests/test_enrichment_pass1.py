"""Tests for enrichment Pass 1 — review history signals."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from zelda.controllers.enrichment import pass1_reviews
from zelda.models.lead import Lead
from zelda.models.lead_enrichment import LeadEnrichment
from zelda.models.review import Review


def _lead(gp_id="gp-1") -> Lead:
    return Lead(
        lead_id="lead-1",
        city="Ludhiana",
        run_id="run-1",
        tier="standalone",
        name="Test Clinic",
        google_places_id=gp_id,
        lybrate_urls=[],
        created_at=datetime.now(timezone.utc),
    )


def _enrichment() -> LeadEnrichment:
    return LeadEnrichment(lead_id="lead-1", city="Ludhiana")


def _review(
    days_ago: int,
    rating: int = 5,
    text: str = "Great clinic",
    has_reply: bool = False,
    reply_days_after: int = 1,
) -> Review:
    now = datetime.now(timezone.utc)
    pub = now - timedelta(days=days_ago)
    reply_at = (pub + timedelta(days=reply_days_after)) if has_reply else None
    return Review(
        review_id=f"r-{days_ago}-{rating}",
        place_id="gp-1",
        rating=rating,
        text=text,
        approx_publish_at=pub,
        owner_response_text="Thank you!" if has_reply else None,
        owner_response_approx_at=reply_at,
    )


class TestReviewVelocity:
    def test_counts_within_windows(self):
        reviews = [
            _review(days_ago=10),
            _review(days_ago=25),
            _review(days_ago=60),
            _review(days_ago=100),
            _review(days_ago=200),
        ]
        repo = MagicMock()
        repo.get_reviews_for_place.return_value = reviews

        result = pass1_reviews.run(_lead(), _enrichment(), review_repo=repo)

        assert result.review_velocity_30d == 2
        assert result.review_velocity_90d == 3
        assert result.review_velocity_180d == 4

    def test_zero_velocity_stale_practice(self):
        reviews = [_review(days_ago=400), _review(days_ago=500)]
        repo = MagicMock()
        repo.get_reviews_for_place.return_value = reviews

        result = pass1_reviews.run(_lead(), _enrichment(), review_repo=repo)

        assert result.review_velocity_30d == 0
        assert result.review_velocity_90d == 0
        assert result.review_velocity_180d == 0

    def test_no_reviews_returns_zeros(self):
        repo = MagicMock()
        repo.get_reviews_for_place.return_value = []

        result = pass1_reviews.run(_lead(), _enrichment(), review_repo=repo)

        assert result.review_velocity_30d == 0

    def test_no_gp_id_returns_zeros(self):
        repo = MagicMock()
        result = pass1_reviews.run(
            _lead(gp_id=None), _enrichment(), review_repo=repo
        )
        assert result.review_velocity_30d == 0
        repo.get_reviews_for_place.assert_not_called()


class TestOwnerResponse:
    def test_response_rate(self):
        reviews = [
            _review(10, has_reply=True),
            _review(20, has_reply=True),
            _review(30, has_reply=False),
            _review(40, has_reply=False),
        ]
        repo = MagicMock()
        repo.get_reviews_for_place.return_value = reviews

        result = pass1_reviews.run(_lead(), _enrichment(), review_repo=repo)

        assert result.owner_response_rate == 0.5

    def test_zero_response_rate(self):
        reviews = [_review(10), _review(20)]
        repo = MagicMock()
        repo.get_reviews_for_place.return_value = reviews

        result = pass1_reviews.run(_lead(), _enrichment(), review_repo=repo)

        assert result.owner_response_rate == 0.0

    def test_avg_response_days(self):
        reviews = [
            _review(10, has_reply=True, reply_days_after=2),
            _review(20, has_reply=True, reply_days_after=4),
        ]
        repo = MagicMock()
        repo.get_reviews_for_place.return_value = reviews

        result = pass1_reviews.run(_lead(), _enrichment(), review_repo=repo)

        assert result.owner_avg_response_days == 3.0


class TestRevenueLeak:
    @pytest.mark.parametrize("text", [
        "They didn't pick up the phone",
        "No reply from the clinic",
        "Not picking up calls",
        "Phone not answered multiple times",
        "Could not reach them",
        "They never responded to my WhatsApp",
    ])
    def test_detects_revenue_leak(self, text):
        repo = MagicMock()
        repo.get_reviews_for_place.return_value = [_review(5, text=text)]

        result = pass1_reviews.run(_lead(), _enrichment(), review_repo=repo)

        assert result.has_revenue_leak_signal is True

    def test_no_leak_in_positive_reviews(self):
        repo = MagicMock()
        repo.get_reviews_for_place.return_value = [
            _review(5, text="Excellent service, very professional"),
            _review(10, text="Quick appointment, great results"),
        ]
        result = pass1_reviews.run(_lead(), _enrichment(), review_repo=repo)
        assert result.has_revenue_leak_signal is False


class TestThemesLLM:
    def test_skips_llm_when_no_low_ratings(self):
        repo = MagicMock()
        repo.get_reviews_for_place.return_value = [
            _review(5, rating=4), _review(10, rating=5)
        ]
        mock_client = MagicMock()
        result = pass1_reviews.run(
            _lead(), _enrichment(), review_repo=repo, anthropic_client=mock_client
        )
        mock_client.messages.create.assert_not_called()
        assert result.negative_theme_flags == []

    def test_calls_llm_for_low_ratings(self):
        repo = MagicMock()
        repo.get_reviews_for_place.return_value = [
            _review(5, rating=2, text="Long wait and rude staff")
        ]
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_block = MagicMock()
        mock_block.type = "tool_use"
        mock_block.input = {
            "no_reply": False, "wait_time": True, "billing": False,
            "hygiene": False, "pain": False, "rude_staff": True,
        }
        mock_response.content = [mock_block]
        mock_client.messages.create.return_value = mock_response

        result = pass1_reviews.run(
            _lead(), _enrichment(), review_repo=repo, anthropic_client=mock_client
        )
        assert "wait_time" in result.negative_theme_flags
        assert "rude_staff" in result.negative_theme_flags

    def test_llm_error_returns_empty_themes(self):
        repo = MagicMock()
        repo.get_reviews_for_place.return_value = [
            _review(5, rating=1, text="Terrible")
        ]
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("API error")

        result = pass1_reviews.run(
            _lead(), _enrichment(), review_repo=repo, anthropic_client=mock_client
        )
        assert result.negative_theme_flags == []


class TestPassMetadata:
    def test_pass_recorded(self):
        repo = MagicMock()
        repo.get_reviews_for_place.return_value = []

        result = pass1_reviews.run(_lead(), _enrichment(), review_repo=repo)

        assert "pass1" in result.passes_completed
