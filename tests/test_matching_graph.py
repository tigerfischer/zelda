"""Tests for Stage 4 — match graph and conflict detection."""

from __future__ import annotations

from datetime import datetime, timezone

from zelda.controllers.matching.graph import (
    MatchGraph,
    build_graph,
)
from zelda.models.match_pair import MatchPairEvaluation


_T = datetime(2026, 4, 30, tzinfo=timezone.utc)


def _eval(
    source_a: str, key_a: str,
    source_b: str, key_b: str,
    *,
    confidence: float = 0.9,
    reason: str = "same address",
) -> MatchPairEvaluation:
    return MatchPairEvaluation(
        source_a=source_a, key_a=key_a,
        source_b=source_b, key_b=key_b,
        stage="reviewer",
        match=True,
        confidence=confidence,
        reason=reason,
        model="claude-sonnet-4-6",
        evaluated_at=_T,
    )


# ── connected components ─────────────────────────────────────────────

def test_single_edge_forms_one_cluster():
    graph = build_graph([_eval("google_places", "gp1", "practo", "pr1")])
    components = graph.connected_components()
    assert len(components) == 1
    cluster = set(components[0])
    assert ("google_places", "gp1") in cluster
    assert ("practo", "pr1") in cluster


def test_two_unconnected_edges_form_two_clusters():
    matches = [
        _eval("google_places", "gp1", "practo", "pr1"),
        _eval("google_places", "gp2", "practo", "pr2"),
    ]
    graph = build_graph(matches)
    assert len(graph.connected_components()) == 2


def test_transitive_edges_form_one_cluster():
    """gp1↔pr1 and pr1↔ly1 → one cluster of three."""
    matches = [
        _eval("google_places", "gp1", "practo", "pr1"),
        _eval("practo", "pr1", "lybrate", "ly1"),
    ]
    graph = build_graph(matches)
    components = graph.connected_components()
    assert len(components) == 1
    assert len(components[0]) == 3


def test_no_matches_produces_no_clusters():
    graph = build_graph([])
    assert graph.connected_components() == []
    assert graph.edge_count == 0


# ── conflict detection ───────────────────────────────────────────────

def test_one_to_many_gp_to_practo_flags_conflict():
    """gp1 matches pr1 AND pr2 → conflict (1-to-many within Practo)."""
    matches = [
        _eval("google_places", "gp1", "practo", "pr1"),
        _eval("google_places", "gp1", "practo", "pr2"),
    ]
    graph = build_graph(matches)
    conflicts = sum(1 for e in graph._edges if e.human_review)
    assert conflicts > 0


def test_lybrate_n_to_one_is_not_a_conflict():
    """Multiple Lybrate rows matching one GP clinic is expected — NOT a conflict."""
    matches = [
        _eval("google_places", "gp1", "lybrate", "ly1"),
        _eval("google_places", "gp1", "lybrate", "ly2"),
        _eval("google_places", "gp1", "lybrate", "ly3"),
    ]
    graph = build_graph(matches)
    conflicts = sum(1 for e in graph._edges if e.human_review)
    assert conflicts == 0  # Lybrate N-to-1 never flagged


def test_clean_gp_practo_match_no_conflict():
    graph = build_graph([_eval("google_places", "gp1", "practo", "pr1")])
    assert all(not e.human_review for e in graph._edges)


# ── edges_for_cluster ────────────────────────────────────────────────

def test_edges_for_cluster_returns_relevant_edges():
    matches = [
        _eval("google_places", "gp1", "practo", "pr1"),
        _eval("google_places", "gp2", "practo", "pr2"),
    ]
    graph = build_graph(matches)
    components = graph.connected_components()
    # Find the cluster containing gp1
    cluster1 = next(c for c in components if ("google_places", "gp1") in c)
    edges = graph.edges_for_cluster(cluster1)
    assert len(edges) == 1
    assert edges[0].key_a == "gp1" or edges[0].key_b == "gp1"


# ── build_graph with proposer_reasons ───────────────────────────────

def test_build_graph_with_proposer_reasons():
    ev = _eval("google_places", "gp1", "practo", "pr1")
    reasons = {("google_places", "gp1", "practo", "pr1"): "shared phone number"}
    graph = build_graph([ev], proposer_reasons=reasons)
    assert graph.edge_count == 1
    edge = graph._edges[0]
    assert edge.proposer_reason == "shared phone number"
