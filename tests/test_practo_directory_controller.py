"""Tests for `PractoDirectoryController` — the layer that runs the
gateway, attaches discovery-time fields, and persists via the repo.

The gateway is mocked here; its own parsing/pagination logic is
covered by `test_practo_directory_gateway.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from zelda.controllers.practo_directory import (
    PractoDirectoryController,
    PractoDirectoryResult,
)
from zelda.gateways.practo_directory import PractoDirectoryEntry
from zelda.repositories.practo_listing_repo import PractoListingRepository


_T_NOW = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)


def _entry(slug: str, name: str = "X Clinic") -> PractoDirectoryEntry:
    return PractoDirectoryEntry(
        profile_url=f"https://www.practo.com/ludhiana/clinic/{slug}",
        name=name,
        address="123 Test St",
        lat=30.9,
        lng=75.85,
    )


@pytest.fixture
def repo():
    r = PractoListingRepository(":memory:")
    yield r
    r.close()


# ── happy path ──────────────────────────────────────────────────────


def test_run_persists_new_entries(repo):
    gw = MagicMock()
    gw.fetch_for_city.return_value = [_entry("a"), _entry("b", "B Clinic")]
    ctrl = PractoDirectoryController(gw, repo, clock=lambda: _T_NOW)

    result = ctrl.run("Ludhiana", run_id="rid-1")

    assert result.discovered == 2
    assert result.inserted == 2
    assert result.already_known == 0
    assert result.errors == []

    rows = repo.get_for_city("Ludhiana")
    assert {r.profile_url for r in rows} == {
        "https://www.practo.com/ludhiana/clinic/a",
        "https://www.practo.com/ludhiana/clinic/b",
    }
    a = repo.get_by_url("https://www.practo.com/ludhiana/clinic/a")
    assert a is not None
    assert a.name == "X Clinic"
    assert a.city == "Ludhiana"
    assert a.discovered_at == _T_NOW
    assert a.lat == 30.9


def test_run_counts_already_known_separately(repo):
    """If a profile_url is already in the DB, it counts toward
    `already_known`, not `inserted`."""
    # Pre-seed one row.
    gw = MagicMock()
    gw.fetch_for_city.return_value = [_entry("a"), _entry("b")]
    ctrl = PractoDirectoryController(gw, repo, clock=lambda: _T_NOW)
    ctrl.run("Ludhiana", run_id="rid-1")  # first run: both inserted

    # Now re-run with the same entries plus one new.
    gw.fetch_for_city.return_value = [_entry("a"), _entry("b"), _entry("c")]
    result = ctrl.run("Ludhiana", run_id="rid-2")

    assert result.discovered == 3
    assert result.already_known == 2
    assert result.inserted == 1


def test_run_is_idempotent_on_replay(repo):
    """Re-running with identical input must not duplicate rows."""
    gw = MagicMock()
    gw.fetch_for_city.return_value = [_entry("a")]
    ctrl = PractoDirectoryController(gw, repo, clock=lambda: _T_NOW)

    ctrl.run("Ludhiana", run_id="rid-1")
    ctrl.run("Ludhiana", run_id="rid-2")

    assert repo.count_for_city("Ludhiana") == 1


def test_run_preserves_discovered_at_on_replay(repo):
    """`discovered_at` must NOT change when an existing row is re-
    upserted in a later run."""
    t1 = datetime(2026, 4, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 30, tzinfo=timezone.utc)
    gw = MagicMock()
    gw.fetch_for_city.return_value = [_entry("a")]

    ctrl1 = PractoDirectoryController(gw, repo, clock=lambda: t1)
    ctrl1.run("Ludhiana", run_id="rid-1")

    ctrl2 = PractoDirectoryController(gw, repo, clock=lambda: t2)
    ctrl2.run("Ludhiana", run_id="rid-2")

    row = repo.get_by_url("https://www.practo.com/ludhiana/clinic/a")
    assert row is not None
    assert row.discovered_at == t1
    assert row.last_modified_at == t2


# ── error containment ──────────────────────────────────────────────


def test_gateway_error_recorded_in_result(repo):
    gw = MagicMock()
    gw.fetch_for_city.side_effect = RuntimeError("akamai blocked us")
    ctrl = PractoDirectoryController(gw, repo, clock=lambda: _T_NOW)

    result = ctrl.run("Ludhiana", run_id="rid-1")

    assert result.discovered == 0
    assert result.inserted == 0
    assert any("akamai blocked" in e for e in result.errors)
    assert repo.count_for_city("Ludhiana") == 0


def test_repo_error_recorded_in_result():
    """Defensive: a repo write failure shouldn't crash the run."""
    gw = MagicMock()
    gw.fetch_for_city.return_value = [_entry("a")]
    repo = MagicMock(spec=PractoListingRepository)
    repo.exists_many.return_value = set()
    repo.upsert_many.side_effect = RuntimeError("disk full")
    ctrl = PractoDirectoryController(gw, repo, clock=lambda: _T_NOW)

    result = ctrl.run("Ludhiana", run_id="rid-1")

    assert any("disk full" in e for e in result.errors)


# ── input validation ───────────────────────────────────────────────


def test_run_rejects_empty_city(repo):
    gw = MagicMock()
    ctrl = PractoDirectoryController(gw, repo)
    with pytest.raises(ValueError, match="city"):
        ctrl.run("", run_id="rid-1")
    with pytest.raises(ValueError):
        ctrl.run("   ", run_id="rid-1")


# ── empty result is valid ──────────────────────────────────────────


def test_empty_directory_returns_clean_result(repo):
    gw = MagicMock()
    gw.fetch_for_city.return_value = []
    ctrl = PractoDirectoryController(gw, repo, clock=lambda: _T_NOW)

    result = ctrl.run("Ludhiana", run_id="rid-1")

    assert result.discovered == 0
    assert result.inserted == 0
    assert result.already_known == 0
    assert result.errors == []
