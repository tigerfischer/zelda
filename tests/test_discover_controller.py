import json
from pathlib import Path
from typing import Any

import pytest

from zelda.controllers.discover import (
    DEFAULT_QUERIES,
    DiscoverController,
)
from zelda.models.place import Place
from zelda.repositories.raw_lead_repo import RawLeadRepository


# ── fakes ────────────────────────────────────────────────────────────────


class FakeGateway:
    """In-memory stand-in for GooglePlacesGateway. Records calls so tests
    can assert on them; raises configured exceptions for failure cases."""

    def __init__(self) -> None:
        self.text_search_results: dict[str, list[Place]] = {}
        self.text_search_failures: dict[str, Exception] = {}
        self.details_responses: dict[str, dict[str, Any]] = {}
        self.details_failures: dict[str, Exception] = {}
        self.text_search_calls: list[tuple[str, int]] = []
        self.details_calls: list[str] = []

    def text_search(self, query: str, *, max_pages: int = 1) -> list[Place]:
        self.text_search_calls.append((query, max_pages))
        if query in self.text_search_failures:
            raise self.text_search_failures[query]
        return list(self.text_search_results.get(query, []))

    def get_place_details(self, place_id: str) -> dict[str, Any]:
        self.details_calls.append(place_id)
        if place_id in self.details_failures:
            raise self.details_failures[place_id]
        if place_id not in self.details_responses:
            raise KeyError(f"no details fixture for {place_id}")
        return dict(self.details_responses[place_id])


# ── helpers ──────────────────────────────────────────────────────────────


def _mk_place(id_: str, name: str = "Test Clinic") -> Place:
    return Place.model_validate(
        {
            "id": id_,
            "displayName": {"text": name, "languageCode": "en"},
            "formattedAddress": "addr",
        }
    )


def _mk_details(id_: str, name: str = "Test Clinic", **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": id_,
        "displayName": {"text": name, "languageCode": "en"},
        "formattedAddress": "Test Address, Ludhiana",
    }
    base.update(extra)
    return base


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def repo():
    r = RawLeadRepository(":memory:")
    yield r
    r.close()


@pytest.fixture
def controller(gateway: FakeGateway, repo: RawLeadRepository, tmp_path: Path) -> DiscoverController:
    return DiscoverController(
        gateway=gateway,
        repo=repo,
        artifacts_dir=tmp_path / "artifacts",
        queries=("dentist in {city}",),  # one query keeps tests focused
    )


# ── search + dedupe ──────────────────────────────────────────────────────


def test_run_calls_text_search_once_per_query_template(gateway, repo, tmp_path):
    controller = DiscoverController(
        gateway=gateway,
        repo=repo,
        artifacts_dir=tmp_path,
        queries=("dentist in {city}", "orthodontist in {city}"),
    )
    gateway.text_search_results = {
        "dentist in Ludhiana": [_mk_place("p1")],
        "orthodontist in Ludhiana": [_mk_place("p2")],
    }
    gateway.details_responses = {
        "p1": _mk_details("p1"),
        "p2": _mk_details("p2"),
    }

    controller.run("Ludhiana")

    assert {q for q, _ in gateway.text_search_calls} == {
        "dentist in Ludhiana",
        "orthodontist in Ludhiana",
    }


def test_run_dedupes_place_ids_across_queries(gateway, repo, tmp_path):
    controller = DiscoverController(
        gateway=gateway,
        repo=repo,
        artifacts_dir=tmp_path,
        queries=("a in {city}", "b in {city}"),
    )
    gateway.text_search_results = {
        "a in Ludhiana": [_mk_place("p1"), _mk_place("p2")],
        "b in Ludhiana": [_mk_place("p2"), _mk_place("p3")],  # p2 is the dupe
    }
    gateway.details_responses = {
        "p1": _mk_details("p1"),
        "p2": _mk_details("p2"),
        "p3": _mk_details("p3"),
    }

    result = controller.run("Ludhiana")

    assert result.text_search_total == 4  # raw count includes the dupe
    assert result.deduped_total == 3      # unique place_ids
    assert sorted(gateway.details_calls) == ["p1", "p2", "p3"]


def test_run_passes_max_pages_to_gateway(gateway, controller):
    gateway.text_search_results = {"dentist in Ludhiana": []}
    controller.run("Ludhiana", max_pages_per_query=3)

    assert gateway.text_search_calls == [("dentist in Ludhiana", 3)]


# ── re-run policy: skip known place_ids ──────────────────────────────────


def test_run_skips_place_ids_already_in_repo(gateway, repo, controller, tmp_path):
    """Re-run policy: place_ids already in DB are skipped — neither the
    Place Details call fires nor any artifact line is written."""
    # Pre-populate one lead via the converter, written through a real upsert.
    from zelda.models.place import raw_lead_from_place_details
    repo.upsert_many([raw_lead_from_place_details(_mk_details("p1"), city="Ludhiana")])

    gateway.text_search_results = {
        "dentist in Ludhiana": [_mk_place("p1"), _mk_place("p2")]
    }
    gateway.details_responses = {"p2": _mk_details("p2")}

    result = controller.run("Ludhiana")

    assert gateway.details_calls == ["p2"]  # p1 not refetched
    assert result.already_known_count == 1
    assert result.new_eligible_count == 1
    assert result.inserted_count == 1


# ── max_results cost cap ─────────────────────────────────────────────────


def test_max_results_caps_details_fetches(gateway, controller):
    """Budget control: with N candidates and max_results=1, exactly one
    Place Details call fires."""
    gateway.text_search_results = {
        "dentist in Ludhiana": [_mk_place(f"p{i}") for i in range(5)]
    }
    gateway.details_responses = {f"p{i}": _mk_details(f"p{i}") for i in range(5)}

    result = controller.run("Ludhiana", max_results=1)

    assert len(gateway.details_calls) == 1
    assert result.after_max_results_count == 1
    assert result.details_fetched_count == 1
    assert result.inserted_count == 1


def test_max_results_zero_makes_no_details_calls(gateway, controller):
    gateway.text_search_results = {
        "dentist in Ludhiana": [_mk_place("p1"), _mk_place("p2")]
    }
    gateway.details_responses = {"p1": _mk_details("p1"), "p2": _mk_details("p2")}

    result = controller.run("Ludhiana", max_results=0)

    assert gateway.details_calls == []
    assert result.details_fetched_count == 0
    assert result.inserted_count == 0


def test_max_results_none_means_unlimited(gateway, controller):
    gateway.text_search_results = {
        "dentist in Ludhiana": [_mk_place(f"p{i}") for i in range(5)]
    }
    gateway.details_responses = {f"p{i}": _mk_details(f"p{i}") for i in range(5)}

    result = controller.run("Ludhiana")  # max_results omitted → None

    assert len(gateway.details_calls) == 5
    assert result.inserted_count == 5


# ── persistence + artifacts ──────────────────────────────────────────────


def test_run_persists_leads_to_repo(gateway, repo, controller):
    gateway.text_search_results = {"dentist in Ludhiana": [_mk_place("p1")]}
    gateway.details_responses = {"p1": _mk_details("p1", name="Lead One")}

    controller.run("Ludhiana")

    lead = repo.get_by_id("p1")
    assert lead is not None
    assert lead.name == "Lead One"
    assert lead.city == "Ludhiana"
    assert lead.raw_json["id"] == "p1"


def test_run_writes_one_jsonl_line_per_fetched_place(gateway, controller, tmp_path):
    gateway.text_search_results = {
        "dentist in Ludhiana": [_mk_place("p1"), _mk_place("p2")]
    }
    gateway.details_responses = {
        "p1": _mk_details("p1", name="Clinic 1"),
        "p2": _mk_details("p2", name="Clinic 2"),
    }

    result = controller.run("Ludhiana")

    assert result.artifact_path is not None
    lines = result.artifact_path.read_text().strip().split("\n")
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    names = {p["displayName"]["text"] for p in parsed}
    assert names == {"Clinic 1", "Clinic 2"}


def test_artifact_path_lives_under_slugified_city_dir(gateway, controller):
    gateway.text_search_results = {"dentist in New Delhi": [_mk_place("p1")]}
    gateway.details_responses = {"p1": _mk_details("p1")}

    result = controller.run("New Delhi")

    assert result.artifact_path is not None
    assert "new-delhi" in str(result.artifact_path).lower()


def test_no_artifact_when_no_new_places(gateway, controller):
    gateway.text_search_results = {"dentist in Ludhiana": []}

    result = controller.run("Ludhiana")

    assert result.artifact_path is None
    assert result.inserted_count == 0


def test_no_artifact_when_all_places_are_known(gateway, repo, controller):
    from zelda.models.place import raw_lead_from_place_details
    repo.upsert_many([raw_lead_from_place_details(_mk_details("p1"), city="Ludhiana")])

    gateway.text_search_results = {"dentist in Ludhiana": [_mk_place("p1")]}

    result = controller.run("Ludhiana")

    assert result.artifact_path is None
    assert gateway.details_calls == []


def test_run_uses_provided_run_id(gateway, controller):
    gateway.text_search_results = {"dentist in Ludhiana": [_mk_place("p1")]}
    gateway.details_responses = {"p1": _mk_details("p1")}

    result = controller.run("Ludhiana", run_id="custom-run-id-001")

    assert result.run_id == "custom-run-id-001"
    assert result.artifact_path is not None
    assert result.artifact_path.name == "custom-run-id-001.jsonl"


# ── error tolerance ──────────────────────────────────────────────────────


def test_text_search_failure_for_one_query_does_not_abort_run(
    gateway, repo, tmp_path
):
    controller = DiscoverController(
        gateway=gateway,
        repo=repo,
        artifacts_dir=tmp_path,
        queries=("a in {city}", "b in {city}"),
    )
    gateway.text_search_failures = {"a in Ludhiana": RuntimeError("api down")}
    gateway.text_search_results = {"b in Ludhiana": [_mk_place("p1")]}
    gateway.details_responses = {"p1": _mk_details("p1")}

    result = controller.run("Ludhiana")

    assert result.inserted_count == 1
    assert any("text_search failed" in e for e in result.errors)


def test_details_failure_for_one_place_does_not_abort_run(gateway, controller):
    gateway.text_search_results = {
        "dentist in Ludhiana": [_mk_place("p1"), _mk_place("p2")]
    }
    gateway.details_failures = {"p1": RuntimeError("place gone")}
    gateway.details_responses = {"p2": _mk_details("p2")}

    result = controller.run("Ludhiana")

    assert result.inserted_count == 1
    assert result.details_fetched_count == 1
    assert any("get_place_details failed" in e for e in result.errors)


def test_all_details_fail_means_no_artifact_no_inserts(gateway, controller):
    gateway.text_search_results = {"dentist in Ludhiana": [_mk_place("p1")]}
    gateway.details_failures = {"p1": RuntimeError("gone")}

    result = controller.run("Ludhiana")

    assert result.artifact_path is None
    assert result.inserted_count == 0
    assert len(result.errors) == 1


# ── input validation ─────────────────────────────────────────────────────


def test_run_rejects_blank_city(controller):
    with pytest.raises(ValueError, match="city"):
        controller.run("")
    with pytest.raises(ValueError, match="city"):
        controller.run("   ")


def test_run_rejects_negative_max_results(controller):
    with pytest.raises(ValueError, match="max_results"):
        controller.run("Ludhiana", max_results=-1)


def test_run_rejects_max_pages_below_one(controller):
    with pytest.raises(ValueError, match="max_pages"):
        controller.run("Ludhiana", max_pages_per_query=0)


# ── default queries shape ────────────────────────────────────────────────


def test_default_queries_all_use_city_placeholder():
    """Sanity check: every default query template must contain `{city}`,
    otherwise it'd error on .format(city=…)."""
    for q in DEFAULT_QUERIES:
        assert "{city}" in q, f"query missing {{city}} placeholder: {q}"
