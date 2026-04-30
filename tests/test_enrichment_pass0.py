"""Tests for enrichment Pass 0 — existing data signals."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from zelda.controllers.enrichment import pass0_existing_data
from zelda.models.google_places_lead import GooglePlacesLead
from zelda.models.lead import Lead
from zelda.models.lead_enrichment import LeadEnrichment
from zelda.models.lybrate_listing import LybrateListing
from zelda.models.practo_listing import PractoListing


def _gp(**kwargs) -> GooglePlacesLead:
    defaults = dict(
        place_id="gp-1",
        city="Ludhiana",
        name="Test Dental Clinic",
        rating=4.2,
        review_count=45,
        website="https://example.com",
        opening_hours={"weekday_text": ["Mon: 9am-5pm"]},
        photos_count=8,
        editorial_summary="A good dental clinic.",
        business_status="OPERATIONAL",
        raw_json={},
        discovered_at=datetime.now(timezone.utc),
        last_modified_at=datetime.now(timezone.utc),
    )
    return GooglePlacesLead(**{**defaults, **kwargs})


def _lead(**kwargs) -> Lead:
    defaults = dict(
        lead_id="lead-1",
        city="Ludhiana",
        run_id="run-1",
        tier="enriched",
        name="Test Dental Clinic",
        google_places_id="gp-1",
        practo_url="https://practo.com/ludhiana/clinic/test",
        lybrate_urls=["https://lybrate.com/ludhiana/dr-sharma"],
        created_at=datetime.now(timezone.utc),
    )
    return Lead(**{**defaults, **kwargs})


def _enrichment() -> LeadEnrichment:
    return LeadEnrichment(lead_id="lead-1", city="Ludhiana")


def _make_repos(gp=None, practo=None, lybrate=None):
    gp_repo = MagicMock()
    gp_repo.get_by_id.return_value = gp

    practo_repo = MagicMock()
    practo_repo.get_by_url.return_value = practo

    lybrate_repo = MagicMock()
    lybrate_repo.get_by_url.return_value = lybrate

    return gp_repo, practo_repo, lybrate_repo


class TestGPSignals:
    def test_review_count_and_rating(self):
        gp_repo, pr, ly = _make_repos(gp=_gp(review_count=42, rating=4.5))
        result = pass0_existing_data.run(
            _lead(), _enrichment(), gp_repo=gp_repo, practo_repo=pr, lybrate_repo=ly
        )
        assert result.google_review_count == 42
        assert result.google_rating == 4.5

    def test_gbp_completeness(self):
        gp_repo, pr, ly = _make_repos(gp=_gp(
            opening_hours={"weekday_text": []},
            photos_count=5,
            editorial_summary="Great clinic",
        ))
        result = pass0_existing_data.run(
            _lead(), _enrichment(), gp_repo=gp_repo, practo_repo=pr, lybrate_repo=ly
        )
        assert result.gbp_has_hours is True
        assert result.gbp_photos_count == 5
        assert result.gbp_has_description is True

    def test_missing_hours_and_description(self):
        gp_repo, pr, ly = _make_repos(gp=_gp(
            opening_hours=None, editorial_summary=None
        ))
        result = pass0_existing_data.run(
            _lead(), _enrichment(), gp_repo=gp_repo, practo_repo=pr, lybrate_repo=ly
        )
        assert result.gbp_has_hours is False
        assert result.gbp_has_description is False

    def test_not_operational(self):
        gp_repo, pr, ly = _make_repos(gp=_gp(business_status="CLOSED_PERMANENTLY"))
        result = pass0_existing_data.run(
            _lead(), _enrichment(), gp_repo=gp_repo, practo_repo=pr, lybrate_repo=ly
        )
        assert result.is_not_operational is True

    def test_operational(self):
        gp_repo, pr, ly = _make_repos(gp=_gp(business_status="OPERATIONAL"))
        result = pass0_existing_data.run(
            _lead(), _enrichment(), gp_repo=gp_repo, practo_repo=pr, lybrate_repo=ly
        )
        assert result.is_not_operational is False


class TestSourcePresence:
    def test_on_practo_and_lybrate(self):
        gp_repo, pr, ly = _make_repos(gp=_gp())
        result = pass0_existing_data.run(
            _lead(practo_url="https://practo.com/x", lybrate_urls=["https://ly.com/x"]),
            _enrichment(),
            gp_repo=gp_repo, practo_repo=pr, lybrate_repo=ly,
        )
        assert result.on_practo is True
        assert result.on_lybrate is True
        assert result.source_count == 3

    def test_standalone_gp_only(self):
        gp_repo, pr, ly = _make_repos(gp=_gp())
        result = pass0_existing_data.run(
            _lead(practo_url=None, lybrate_urls=[]),
            _enrichment(),
            gp_repo=gp_repo, practo_repo=pr, lybrate_repo=ly,
        )
        assert result.on_practo is False
        assert result.on_lybrate is False
        assert result.source_count == 1


class TestChainDetection:
    def test_clove_dental_is_chain(self):
        gp_repo, pr, ly = _make_repos(gp=_gp())
        result = pass0_existing_data.run(
            _lead(name="Clove Dental - Model Town"),
            _enrichment(),
            gp_repo=gp_repo, practo_repo=pr, lybrate_repo=ly,
        )
        assert result.is_chain is True

    def test_independent_clinic_not_chain(self):
        gp_repo, pr, ly = _make_repos(gp=_gp())
        result = pass0_existing_data.run(
            _lead(name="Dr. Sharma's Dental Clinic"),
            _enrichment(),
            gp_repo=gp_repo, practo_repo=pr, lybrate_repo=ly,
        )
        assert result.is_chain is False

    def test_hospital_embedded(self):
        gp_repo, pr, ly = _make_repos(gp=_gp())
        result = pass0_existing_data.run(
            _lead(name="City Hospital Dental Department"),
            _enrichment(),
            gp_repo=gp_repo, practo_repo=pr, lybrate_repo=ly,
        )
        assert result.is_hospital_embedded is True


class TestOwnerFromLybrate:
    def test_picks_up_doctor_name_and_phone(self):
        ly_listing = MagicMock(spec=LybrateListing)
        ly_listing.doctor_name = "Dr. Rajesh Sharma"
        ly_listing.specialty = "BDS, MDS (Orthodontics)"
        ly_listing.phone = "9876543210"

        gp_repo, pr, ly = _make_repos(gp=_gp(), lybrate=ly_listing)
        result = pass0_existing_data.run(
            _lead(), _enrichment(), gp_repo=gp_repo, practo_repo=pr, lybrate_repo=ly
        )
        assert result.owner_name == "Dr. Rajesh Sharma"
        assert result.owner_qualifications == "BDS, MDS (Orthodontics)"
        assert result.direct_phone == "9876543210"

    def test_falls_back_to_lead_phone(self):
        gp_repo, pr, ly = _make_repos(gp=_gp(), lybrate=None)
        result = pass0_existing_data.run(
            _lead(phone="9999999999", lybrate_urls=[]),
            _enrichment(),
            gp_repo=gp_repo, practo_repo=pr, lybrate_repo=ly,
        )
        assert result.direct_phone == "9999999999"


class TestNAPConsistency:
    def test_consistent_phones(self):
        gp = _gp(phone="+91 98765 43210")
        ly_listing = MagicMock(spec=LybrateListing)
        ly_listing.phone = "9876543210"
        ly_listing.doctor_name = "Dr. X"
        ly_listing.specialty = None

        gp_repo, pr, ly = _make_repos(gp=gp, lybrate=ly_listing)
        result = pass0_existing_data.run(
            _lead(), _enrichment(), gp_repo=gp_repo, practo_repo=pr, lybrate_repo=ly
        )
        assert result.nap_consistent is True

    def test_single_source_returns_none(self):
        gp_repo, pr, ly = _make_repos(gp=_gp())
        result = pass0_existing_data.run(
            _lead(practo_url=None, lybrate_urls=[]),
            _enrichment(),
            gp_repo=gp_repo, practo_repo=pr, lybrate_repo=ly,
        )
        # Only one source — can't assess consistency
        assert result.nap_consistent is None

    def test_pass_recorded_in_passes_completed(self):
        gp_repo, pr, ly = _make_repos(gp=_gp())
        result = pass0_existing_data.run(
            _lead(), _enrichment(), gp_repo=gp_repo, practo_repo=pr, lybrate_repo=ly
        )
        assert "pass0" in result.passes_completed
