"""Tests for `LybrateDirectoryController` — same shape as the Practo
controller. Gateway is mocked; its parsing logic is covered by
`test_lybrate_directory_gateway.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from zelda.controllers.lybrate_directory import (
    LybrateDirectoryController,
    LybrateDirectoryResult,
)
from zelda.gateways.lybrate_directory import LybrateDirectoryEntry
from zelda.repositories.lybrate_listing_repo import LybrateListingRepository


_T_NOW = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)


def _entry(slug: str, name: str = "Dr X") -> LybrateDirectoryEntry:
    return LybrateDirectoryEntry(
        profile_url=f"https://www.lybrate.com/ludhiana/doctor/{slug}",
        doctor_name=name,
        address="123 Test St",
        locality="Model Town",
        postal_code="141001",
        lat=30.9,
        lng=75.85,
        specialty="Dentist",
    )


@pytest.fixture
def repo():
    r = LybrateListingRepository(":memory:")
    yield r
    r.close()


def test_run_persists_new_entries(repo):
    gw = MagicMock()
    gw.fetch_for_city.return_value = [_entry("a"), _entry("b", "Dr Y")]
    ctrl = LybrateDirectoryController(gw, repo, clock=lambda: _T_NOW)

    result = ctrl.run("Ludhiana", run_id="rid-1")

    assert result.discovered == 2
    assert result.inserted == 2
    assert result.already_known == 0
    assert result.errors == []

    rows = repo.get_for_city("Ludhiana")
    assert {r.profile_url for r in rows} == {
        "https://www.lybrate.com/ludhiana/doctor/a",
        "https://www.lybrate.com/ludhiana/doctor/b",
    }
    a = repo.get_by_url("https://www.lybrate.com/ludhiana/doctor/a")
    assert a is not None
    assert a.doctor_name == "Dr X"
    assert a.specialty == "Dentist"
    assert a.locality == "Model Town"
    assert a.lat == 30.9


def test_clinic_name_and_phone_are_null_at_discovery(repo):
    """Listing JSON-LD doesn't expose clinic_name or phone — both
    should be None on the persisted row."""
    gw = MagicMock()
    gw.fetch_for_city.return_value = [_entry("a")]
    ctrl = LybrateDirectoryController(gw, repo, clock=lambda: _T_NOW)
    ctrl.run("Ludhiana", run_id="rid-1")

    a = repo.get_by_url("https://www.lybrate.com/ludhiana/doctor/a")
    assert a is not None
    assert a.clinic_name is None
    assert a.phone is None


def test_run_counts_already_known_separately(repo):
    gw = MagicMock()
    gw.fetch_for_city.return_value = [_entry("a"), _entry("b")]
    ctrl = LybrateDirectoryController(gw, repo, clock=lambda: _T_NOW)
    ctrl.run("Ludhiana", run_id="rid-1")

    gw.fetch_for_city.return_value = [_entry("a"), _entry("b"), _entry("c")]
    result = ctrl.run("Ludhiana", run_id="rid-2")

    assert result.discovered == 3
    assert result.already_known == 2
    assert result.inserted == 1


def test_run_is_idempotent(repo):
    gw = MagicMock()
    gw.fetch_for_city.return_value = [_entry("a")]
    ctrl = LybrateDirectoryController(gw, repo, clock=lambda: _T_NOW)
    ctrl.run("Ludhiana", run_id="rid-1")
    ctrl.run("Ludhiana", run_id="rid-2")
    assert repo.count_for_city("Ludhiana") == 1


def test_gateway_error_recorded_in_result(repo):
    gw = MagicMock()
    gw.fetch_for_city.side_effect = RuntimeError("network down")
    ctrl = LybrateDirectoryController(gw, repo, clock=lambda: _T_NOW)

    result = ctrl.run("Ludhiana", run_id="rid-1")

    assert result.discovered == 0
    assert result.inserted == 0
    assert any("network down" in e for e in result.errors)


def test_run_rejects_empty_city(repo):
    gw = MagicMock()
    ctrl = LybrateDirectoryController(gw, repo)
    with pytest.raises(ValueError, match="city"):
        ctrl.run("", run_id="rid-1")


def test_empty_directory_returns_clean_result(repo):
    gw = MagicMock()
    gw.fetch_for_city.return_value = []
    ctrl = LybrateDirectoryController(gw, repo, clock=lambda: _T_NOW)

    result = ctrl.run("Ludhiana", run_id="rid-1")

    assert result.discovered == 0
    assert result.inserted == 0
    assert result.already_known == 0
    assert result.errors == []
