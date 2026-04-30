"""Tests for the EnrichmentOrchestrator.

Uses fake `SourceAdapter` implementations so the loop logic
(can_fetch / is_cached_fresh / fetch_for_lead / blocking semantics
/ rate limiting) is testable in isolation, independently of the
concrete adapters which have their own test file.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from zelda.controllers.enrichment_orchestrator import (
    EnrichmentOrchestrator,
    OrchestratorResult,
)
from zelda.models.google_places_lead import GooglePlacesLead
from zelda.repositories.google_places_lead_repo import GooglePlacesLeadRepository


_T_NOW = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)


# ── fake source adapter ────────────────────────────────────────────────


@dataclass
class FakeSource:
    """Programmable SourceAdapter for tests. Each predicate /
    fetch behaviour is configurable per-place_id."""

    name: str
    can_fetch_default: bool = True
    is_fresh_default: bool = False

    can_fetch_overrides: dict[str, bool] = field(default_factory=dict)
    is_fresh_overrides: dict[str, bool] = field(default_factory=dict)
    # Maps place_id → fetch_status to return ("ok" | "blocked" | "error" |
    # "not_found" | "partial").
    fetch_responses: dict[str, str] = field(default_factory=dict)
    fetch_default_status: str = "ok"
    raise_on_fetch: bool = False

    can_fetch_calls: list[str] = field(default_factory=list)
    is_fresh_calls: list[tuple[str, float]] = field(default_factory=list)
    fetch_calls: list[dict[str, Any]] = field(default_factory=list)

    def can_fetch(self, lead: GooglePlacesLead) -> bool:
        self.can_fetch_calls.append(lead.place_id)
        return self.can_fetch_overrides.get(
            lead.place_id, self.can_fetch_default,
        )

    def is_cached_fresh(self, place_id, *, max_age_days, now):
        self.is_fresh_calls.append((place_id, max_age_days))
        return self.is_fresh_overrides.get(place_id, self.is_fresh_default)

    def fetch_for_lead(self, lead, *, capture_id, now):
        self.fetch_calls.append({
            "place_id": lead.place_id,
            "capture_id": capture_id,
        })
        if self.raise_on_fetch:
            raise RuntimeError(f"forced raise for {lead.place_id}")
        status = self.fetch_responses.get(
            lead.place_id, self.fetch_default_status,
        )
        return {
            "source": self.name,
            "place_id": lead.place_id,
            "fetch_status": status,
            "capture_id": capture_id,
            "error_message": (
                f"injected {status}" if status not in {"ok", "partial"} else None
            ),
        }


# ── helpers + fixtures ──────────────────────────────────────────────────


def _mk_lead(place_id: str = "ChIJ_X", city: str = "Ludhiana") -> GooglePlacesLead:
    return GooglePlacesLead(
        place_id=place_id,
        city=city,
        name=f"Clinic {place_id}",
        review_count=50,
        discovered_at=_T_NOW,
        last_modified_at=_T_NOW,
    )


@pytest.fixture
def lead_repo():
    r = GooglePlacesLeadRepository(":memory:")
    yield r
    r.close()


def _make_orchestrator(
    sources, lead_repo, *, sleep_calls=None,
) -> EnrichmentOrchestrator:
    return EnrichmentOrchestrator(
        sources=sources,
        lead_repo=lead_repo,
        clock=lambda: _T_NOW,
        sleeper=(sleep_calls.append if sleep_calls is not None else (lambda _: None)),
        inter_lead_delay_range=(0.0, 0.0),
    )


# ── construction validation ────────────────────────────────────────────


def test_orchestrator_rejects_empty_sources(lead_repo):
    with pytest.raises(ValueError, match="non-empty"):
        EnrichmentOrchestrator(sources=[], lead_repo=lead_repo)


def test_orchestrator_rejects_duplicate_source_names(lead_repo):
    with pytest.raises(ValueError, match="duplicate"):
        EnrichmentOrchestrator(
            sources=[FakeSource("dup"), FakeSource("dup")],
            lead_repo=lead_repo,
        )


# ── enrich_city input validation ──────────────────────────────────────


def test_enrich_city_rejects_blank_city(lead_repo):
    o = _make_orchestrator([FakeSource("a")], lead_repo)
    with pytest.raises(ValueError, match="city"):
        o.enrich_city("")
    with pytest.raises(ValueError, match="city"):
        o.enrich_city("   ")


def test_enrich_city_rejects_negative_max_leads(lead_repo):
    o = _make_orchestrator([FakeSource("a")], lead_repo)
    with pytest.raises(ValueError, match="max_leads"):
        o.enrich_city("Ludhiana", max_leads=-1)


def test_enrich_city_rejects_negative_max_age_days(lead_repo):
    o = _make_orchestrator([FakeSource("a")], lead_repo)
    with pytest.raises(ValueError, match="max_age_days"):
        o.enrich_city("Ludhiana", max_age_days=-1.0)


def test_enrich_city_rejects_unknown_source_name(lead_repo):
    o = _make_orchestrator([FakeSource("a")], lead_repo)
    with pytest.raises(ValueError, match="unknown source"):
        o.enrich_city("Ludhiana", only_sources=["bogus"])


# ── empty / no-op ──────────────────────────────────────────────────────


def test_enrich_city_with_no_leads_is_noop(lead_repo):
    src = FakeSource("a")
    o = _make_orchestrator([src], lead_repo)
    result = o.enrich_city("Ludhiana")
    assert result.n_leads_in_city == 0
    assert result.n_after_max_leads == 0
    assert src.fetch_calls == []
    assert src.can_fetch_calls == []


# ── happy path: lead × source matrix ──────────────────────────────────


def test_processes_each_lead_against_each_source(lead_repo):
    lead_repo.upsert_many([_mk_lead("p1"), _mk_lead("p2")])
    src_a = FakeSource("a")
    src_b = FakeSource("b")
    o = _make_orchestrator([src_a, src_b], lead_repo)

    result = o.enrich_city("Ludhiana")

    assert result.n_after_max_leads == 2
    assert {c["place_id"] for c in src_a.fetch_calls} == {"p1", "p2"}
    assert {c["place_id"] for c in src_b.fetch_calls} == {"p1", "p2"}
    assert result.by_source["a"].n_successful == 2
    assert result.by_source["b"].n_successful == 2
    assert len(result.captures) == 4  # 2 leads × 2 sources


def test_only_sources_filter_runs_subset(lead_repo):
    lead_repo.upsert_many([_mk_lead("p1")])
    src_a = FakeSource("a")
    src_b = FakeSource("b")
    o = _make_orchestrator([src_a, src_b], lead_repo)

    o.enrich_city("Ludhiana", only_sources=["a"])

    assert len(src_a.fetch_calls) == 1
    assert len(src_b.fetch_calls) == 0


def test_max_leads_caps_processing(lead_repo):
    lead_repo.upsert_many([_mk_lead(f"p{i}") for i in range(5)])
    src = FakeSource("a")
    o = _make_orchestrator([src], lead_repo)

    result = o.enrich_city("Ludhiana", max_leads=2)

    assert result.n_leads_in_city == 5
    assert result.n_after_max_leads == 2
    assert len(src.fetch_calls) == 2


def test_max_leads_zero_means_no_processing(lead_repo):
    lead_repo.upsert_many([_mk_lead("p1"), _mk_lead("p2")])
    src = FakeSource("a")
    o = _make_orchestrator([src], lead_repo)
    result = o.enrich_city("Ludhiana", max_leads=0)
    assert len(src.fetch_calls) == 0
    assert result.n_after_max_leads == 0


# ── cache logic ────────────────────────────────────────────────────────


def test_cache_hit_skips_fetch(lead_repo):
    lead_repo.upsert_many([_mk_lead("p1"), _mk_lead("p2")])
    src = FakeSource("a", is_fresh_default=False)
    src.is_fresh_overrides["p1"] = True
    o = _make_orchestrator([src], lead_repo)

    result = o.enrich_city("Ludhiana")

    assert {c["place_id"] for c in src.fetch_calls} == {"p2"}
    assert result.by_source["a"].n_cache_hits == 1
    assert result.by_source["a"].n_attempted == 1


def test_force_refresh_bypasses_cache(lead_repo):
    lead_repo.upsert_many([_mk_lead("p1")])
    src = FakeSource("a", is_fresh_default=True)
    o = _make_orchestrator([src], lead_repo)

    result = o.enrich_city("Ludhiana", force_refresh=True)

    assert len(src.fetch_calls) == 1
    assert result.by_source["a"].n_cache_hits == 0
    assert result.by_source["a"].n_successful == 1


def test_passes_max_age_days_to_is_cached_fresh(lead_repo):
    lead_repo.upsert_many([_mk_lead("p1")])
    src = FakeSource("a")
    o = _make_orchestrator([src], lead_repo)

    o.enrich_city("Ludhiana", max_age_days=42.0)

    assert src.is_fresh_calls == [("p1", 42.0)]


def test_default_max_age_days_is_180(lead_repo):
    """6-month default per project decision."""
    lead_repo.upsert_many([_mk_lead("p1")])
    src = FakeSource("a")
    o = _make_orchestrator([src], lead_repo)

    o.enrich_city("Ludhiana")

    assert src.is_fresh_calls == [("p1", 180.0)]


# ── can_fetch / no-prerequisite-data path ──────────────────────────────


def test_can_fetch_false_skips_fetch_with_no_prereq_count(lead_repo):
    lead_repo.upsert_many([_mk_lead("p1"), _mk_lead("p2")])
    src = FakeSource("a", can_fetch_default=False)
    o = _make_orchestrator([src], lead_repo)

    result = o.enrich_city("Ludhiana")

    assert src.fetch_calls == []
    assert result.by_source["a"].n_no_prereq == 2
    assert result.by_source["a"].n_attempted == 0


def test_one_source_can_fetch_other_cannot(lead_repo):
    """The realistic case: reviews always fetchable, Practo only when
    URL stub exists."""
    lead_repo.upsert_many([_mk_lead("p1"), _mk_lead("p2")])
    reviews = FakeSource("google_reviews", can_fetch_default=True)
    practo = FakeSource("practo_profile", can_fetch_default=False)
    practo.can_fetch_overrides["p1"] = True  # only p1 has a Practo URL
    o = _make_orchestrator([reviews, practo], lead_repo)

    o.enrich_city("Ludhiana")

    # Reviews ran for both
    assert {c["place_id"] for c in reviews.fetch_calls} == {"p1", "p2"}
    # Practo only ran for p1
    assert {c["place_id"] for c in practo.fetch_calls} == {"p1"}


# ── blocking semantics ─────────────────────────────────────────────────


def test_blocked_status_disables_only_that_source_for_remaining_leads(lead_repo):
    """The whole point of per-source blocking: if Google blocks our
    reviews scraper, Practo (different domain) keeps running."""
    lead_repo.upsert_many([_mk_lead("p1"), _mk_lead("p2"), _mk_lead("p3")])
    reviews = FakeSource("google_reviews")
    reviews.fetch_responses["p1"] = "ok"
    reviews.fetch_responses["p2"] = "blocked"
    reviews.fetch_responses["p3"] = "ok"  # would be ok but reviews is now disabled
    practo = FakeSource("practo_profile")
    o = _make_orchestrator([reviews, practo], lead_repo)

    result = o.enrich_city("Ludhiana")

    # Reviews ran for p1 (ok) and p2 (blocked), then skipped p3
    assert [c["place_id"] for c in reviews.fetch_calls] == ["p1", "p2"]
    # Practo ran for ALL three — it wasn't affected by reviews being blocked
    assert [c["place_id"] for c in practo.fetch_calls] == ["p1", "p2", "p3"]
    assert result.by_source["google_reviews"].n_successful == 1
    assert result.by_source["google_reviews"].n_blocked == 1
    assert result.by_source["google_reviews"].n_skipped_blocked_earlier == 1
    assert result.by_source["practo_profile"].n_successful == 3
    assert "google_reviews" in result.blocked_sources


def test_captcha_status_also_blocks(lead_repo):
    lead_repo.upsert_many([_mk_lead("p1"), _mk_lead("p2")])
    src = FakeSource("a")
    src.fetch_responses["p1"] = "captcha"
    o = _make_orchestrator([src], lead_repo)

    result = o.enrich_city("Ludhiana")

    assert result.by_source["a"].n_blocked == 1
    assert result.by_source["a"].n_skipped_blocked_earlier == 1
    assert "a" in result.blocked_sources


# ── error handling ────────────────────────────────────────────────────


def test_error_status_does_not_block_continues_run(lead_repo):
    lead_repo.upsert_many([_mk_lead("p1"), _mk_lead("p2")])
    src = FakeSource("a")
    src.fetch_responses["p1"] = "error"
    o = _make_orchestrator([src], lead_repo)

    result = o.enrich_city("Ludhiana")

    assert len(src.fetch_calls) == 2  # both leads attempted
    assert result.by_source["a"].n_errored == 1
    assert result.by_source["a"].n_successful == 1


def test_not_found_status_counted_as_other_terminal(lead_repo):
    """Practo's `not_found` is a terminal but non-error outcome."""
    lead_repo.upsert_many([_mk_lead("p1")])
    src = FakeSource("practo_profile")
    src.fetch_responses["p1"] = "not_found"
    o = _make_orchestrator([src], lead_repo)

    result = o.enrich_city("Ludhiana")

    assert result.by_source["practo_profile"].n_other_terminal == 1
    assert result.by_source["practo_profile"].n_errored == 0


def test_adapter_exception_is_caught_and_recorded(lead_repo):
    """Adapters MUST NOT raise per contract, but defend defensively."""
    lead_repo.upsert_many([_mk_lead("p1"), _mk_lead("p2")])
    src = FakeSource("a", raise_on_fetch=True)
    o = _make_orchestrator([src], lead_repo)

    result = o.enrich_city("Ludhiana")

    assert result.by_source["a"].n_errored == 2
    assert result.by_source["a"].n_attempted == 2
    assert any("forced raise" in e for e in result.errors)


def test_unknown_status_counted_as_error(lead_repo):
    """Defensive: an adapter returning a status outside our known sets
    should not silently inflate any other counter."""
    lead_repo.upsert_many([_mk_lead("p1")])
    src = FakeSource("a")
    src.fetch_responses["p1"] = "weird_new_status"
    o = _make_orchestrator([src], lead_repo)

    result = o.enrich_city("Ludhiana")

    assert result.by_source["a"].n_errored == 1


# ── inter-lead rate limiting ───────────────────────────────────────────


def test_sleeps_between_leads_but_not_before_first(lead_repo):
    lead_repo.upsert_many([_mk_lead(f"p{i}") for i in range(3)])
    sleep_calls: list[float] = []
    o = EnrichmentOrchestrator(
        sources=[FakeSource("a")],
        lead_repo=lead_repo,
        clock=lambda: _T_NOW,
        sleeper=sleep_calls.append,
        inter_lead_delay_range=(5.0, 5.0),
    )

    o.enrich_city("Ludhiana")

    # 3 leads → 2 inter-lead sleeps (after #1, after #2; not before #1)
    assert sleep_calls == [5.0, 5.0]


def test_default_inter_lead_delay_range_is_8_to_20(lead_repo):
    """Same as the Reviews controller's original default — orchestrator
    has now taken over inter-place pacing since we bypass run()."""
    o = EnrichmentOrchestrator(
        sources=[FakeSource("a")],
        lead_repo=lead_repo,
    )
    assert o._inter_lead_delay_range == (8.0, 20.0)  # noqa: SLF001


# ── result shape ───────────────────────────────────────────────────────


def test_result_has_per_source_stats_for_every_active_source(lead_repo):
    lead_repo.upsert_many([_mk_lead("p1")])
    src_a = FakeSource("a")
    src_b = FakeSource("b")
    o = _make_orchestrator([src_a, src_b], lead_repo)

    result = o.enrich_city("Ludhiana")

    assert set(result.by_source.keys()) == {"a", "b"}


def test_only_sources_filter_results_in_only_those_in_by_source(lead_repo):
    lead_repo.upsert_many([_mk_lead("p1")])
    o = _make_orchestrator([FakeSource("a"), FakeSource("b")], lead_repo)

    result = o.enrich_city("Ludhiana", only_sources=["a"])

    assert set(result.by_source.keys()) == {"a"}


def test_captures_carry_source_identifier(lead_repo):
    lead_repo.upsert_many([_mk_lead("p1")])
    o = _make_orchestrator([FakeSource("a"), FakeSource("b")], lead_repo)

    result = o.enrich_city("Ludhiana")

    sources_in_captures = {c["source"] for c in result.captures}
    assert sources_in_captures == {"a", "b"}


def test_run_id_can_be_provided(lead_repo):
    lead_repo.upsert_many([_mk_lead("p1")])
    o = _make_orchestrator([FakeSource("a")], lead_repo)

    result = o.enrich_city("Ludhiana", run_id="custom-run-id")

    assert result.run_id == "custom-run-id"
    # capture_id derives from run_id
    assert result.captures[0]["capture_id"].startswith("custom-run-id")
