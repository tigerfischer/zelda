"""Tests for enrichment Pass 2 — website audit signals."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from zelda.controllers.enrichment import pass2_website
from zelda.models.lead import Lead
from zelda.models.lead_enrichment import LeadEnrichment


def _lead(website="https://example.com", google_places_id=None) -> Lead:
    return Lead(
        lead_id="lead-1",
        city="Ludhiana",
        run_id="run-1",
        tier="standalone",
        name="Test Dental Clinic",
        website=website,
        google_places_id=google_places_id,
        lybrate_urls=[],
        created_at=datetime.now(timezone.utc),
    )


def _enrichment() -> LeadEnrichment:
    return LeadEnrichment(lead_id="lead-1", city="Ludhiana")


def _make_audit(**kwargs) -> dict:
    defaults = {
        "website_loads": True,
        "is_mobile_friendly": True,
        "has_schema_markup": False,
        "has_blog": False,
        "has_whatsapp_link": False,
        "has_online_booking": False,
        "has_chat_widget": False,
        "agency_credit": None,
        "page_text": "General dentistry implants orthodontics",
        "error": None,
    }
    return {**defaults, **kwargs}


class TestNoWebsite:
    def test_skips_when_no_website(self):
        gp_repo = MagicMock()
        gp_repo.get_by_id.return_value = None
        gateway = MagicMock()

        result = pass2_website.run(
            _lead(website=None), _enrichment(),
            gp_repo=gp_repo, gateway=gateway,
        )

        gateway.audit.assert_not_called()
        assert result.website_loads is None
        assert "pass2" in result.passes_completed

    def test_falls_back_to_gp_website(self):
        gp = MagicMock()
        gp.website = "https://gp-website.com"
        gp_repo = MagicMock()
        gp_repo.get_by_id.return_value = gp
        gateway = MagicMock()
        gateway.audit.return_value = _make_audit()

        result = pass2_website.run(
            _lead(website=None, google_places_id="gp-1"), _enrichment(),
            gp_repo=gp_repo, gateway=gateway,
        )

        gateway.audit.assert_called_once_with("https://gp-website.com")


class TestWebsiteSignals:
    def test_all_positive_signals(self):
        gp_repo = MagicMock()
        gateway = MagicMock()
        gateway.audit.return_value = _make_audit(
            website_loads=True,
            is_mobile_friendly=True,
            has_schema_markup=True,
            has_blog=True,
            has_whatsapp_link=True,
            has_online_booking=True,
            has_chat_widget=True,
            agency_credit="WebAgency India",
        )

        result = pass2_website.run(
            _lead(), _enrichment(), gp_repo=gp_repo, gateway=gateway,
        )

        assert result.website_loads is True
        assert result.website_is_mobile_friendly is True
        assert result.website_has_schema_markup is True
        assert result.website_has_blog is True
        assert result.has_whatsapp_link is True
        assert result.has_online_booking is True
        assert result.has_chat_widget is True
        assert result.website_agency_credit == "WebAgency India"

    def test_failed_load_stops_early(self):
        gp_repo = MagicMock()
        gateway = MagicMock()
        gateway.audit.return_value = {
            "website_loads": False,
            "error": "timeout",
        }

        result = pass2_website.run(
            _lead(), _enrichment(), gp_repo=gp_repo, gateway=gateway,
        )

        assert result.website_loads is False
        assert result.website_is_mobile_friendly is None
        assert "pass2" in result.passes_completed


class TestEquipmentKeywords:
    def test_detects_cbct(self):
        gp_repo = MagicMock()
        gateway = MagicMock()
        gateway.audit.return_value = _make_audit(
            page_text="We use CBCT for accurate diagnosis and OPG X-ray"
        )

        result = pass2_website.run(
            _lead(), _enrichment(), gp_repo=gp_repo, gateway=gateway,
        )

        assert "cbct" in result.equipment_claims
        assert "opg" in result.equipment_claims

    def test_detects_laser(self):
        gp_repo = MagicMock()
        gateway = MagicMock()
        gateway.audit.return_value = _make_audit(
            page_text="Dental laser treatment available"
        )

        result = pass2_website.run(
            _lead(), _enrichment(), gp_repo=gp_repo, gateway=gateway,
        )

        assert "laser" in result.equipment_claims


class TestServiceMixLLM:
    def test_calls_llm_for_service_classification(self):
        gp_repo = MagicMock()
        gateway = MagicMock()
        gateway.audit.return_value = _make_audit(
            page_text="We offer implants and orthodontics"
        )
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_block = MagicMock()
        mock_block.type = "tool_use"
        mock_block.input = {"services": ["implants", "orthodontics"], "equipment": []}
        mock_response.content = [mock_block]
        mock_client.messages.create.return_value = mock_response

        result = pass2_website.run(
            _lead(), _enrichment(),
            gp_repo=gp_repo, gateway=gateway,
            anthropic_client=mock_client,
        )

        assert "implants" in result.service_mix
        assert "orthodontics" in result.service_mix

    def test_llm_error_returns_empty_service_mix(self):
        gp_repo = MagicMock()
        gateway = MagicMock()
        gateway.audit.return_value = _make_audit(page_text="Some text")
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("API error")

        result = pass2_website.run(
            _lead(), _enrichment(),
            gp_repo=gp_repo, gateway=gateway,
            anthropic_client=mock_client,
        )

        assert result.service_mix == []
