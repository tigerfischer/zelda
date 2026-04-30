"""Stage 4 — match graph and conflict detection.

Confirmed matches from the Reviewer are edges in an undirected graph.
Nodes are (source, key) pairs.

Connected components become clusters — each cluster is a set of rows
from potentially all three sources that the LLMs agreed refer to the
same physical clinic.

Conflict detection
------------------
A **1-to-many conflict** occurs when a single row from source A matches
two or more rows from the same source B. Example: one Google Places
clinic matched to two different Practo listings — this is suspicious and
gets flagged for human review.

The N-to-1 case for Lybrate is NOT a conflict. Multiple Lybrate doctor
rows matching one GP/Practo clinic is expected (doctor-keyed vs
clinic-keyed). These clusters are handled normally by synthesis.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from zelda.models.match_pair import MatchPairEvaluation
from zelda.models.matchable_row import MatchableRow


Node = tuple[str, str]  # (source, key)


@dataclass
class MatchEdge:
    source_a: str
    key_a: str
    source_b: str
    key_b: str
    confidence: float
    proposer_reason: str
    reviewer_reason: str
    human_review: bool = False


@dataclass
class MatchGraph:
    """Undirected graph of confirmed matches."""

    _edges: list[MatchEdge] = field(default_factory=list)
    _adjacency: dict[Node, set[Node]] = field(
        default_factory=lambda: defaultdict(set)
    )

    def add_confirmed_match(self, evaluation: MatchPairEvaluation, proposer_reason: str = "") -> None:
        node_a: Node = (evaluation.source_a, evaluation.key_a)
        node_b: Node = (evaluation.source_b, evaluation.key_b)
        edge = MatchEdge(
            source_a=evaluation.source_a,
            key_a=evaluation.key_a,
            source_b=evaluation.source_b,
            key_b=evaluation.key_b,
            confidence=evaluation.confidence or 0.0,
            proposer_reason=proposer_reason,
            reviewer_reason=evaluation.reason,
        )
        self._edges.append(edge)
        self._adjacency[node_a].add(node_b)
        self._adjacency[node_b].add(node_a)

    def flag_conflicts(self) -> int:
        """Mark edges involved in 1-to-many conflicts. Returns conflict count."""
        conflicts = 0
        for edge in self._edges:
            node_a: Node = (edge.source_a, edge.key_a)
            node_b: Node = (edge.source_b, edge.key_b)

            # Check node_a's neighbours for multiple matches from the same source.
            if _has_multi_source_conflict(node_a, self._adjacency, edge.source_b):
                edge.human_review = True
                conflicts += 1

            # Check node_b's neighbours for multiple matches from the same source.
            if _has_multi_source_conflict(node_b, self._adjacency, edge.source_a):
                edge.human_review = True
                conflicts += 1

        return conflicts

    def connected_components(self) -> list[list[Node]]:
        """Return list of connected components (clusters)."""
        visited: set[Node] = set()
        components: list[list[Node]] = []

        all_nodes = set(self._adjacency.keys())
        for node in all_nodes:
            if node in visited:
                continue
            component: list[Node] = []
            queue = [node]
            while queue:
                n = queue.pop()
                if n in visited:
                    continue
                visited.add(n)
                component.append(n)
                queue.extend(self._adjacency[n] - visited)
            components.append(component)

        return components

    def edges_for_cluster(self, cluster: list[Node]) -> list[MatchEdge]:
        cluster_set = set(cluster)
        return [
            e for e in self._edges
            if (e.source_a, e.key_a) in cluster_set
            and (e.source_b, e.key_b) in cluster_set
        ]

    @property
    def edge_count(self) -> int:
        return len(self._edges)

    @property
    def node_count(self) -> int:
        return len(self._adjacency)


def build_graph(
    confirmed_matches: list[MatchPairEvaluation],
    proposer_reasons: dict[tuple[str, str, str, str], str] | None = None,
) -> MatchGraph:
    """Build a MatchGraph from a list of reviewer-confirmed evaluations.

    `proposer_reasons` maps (source_a, key_a, source_b, key_b) → proposer reason
    for richer cluster context. Optional.
    """
    graph = MatchGraph()
    for ev in confirmed_matches:
        pr = ""
        if proposer_reasons:
            pr = proposer_reasons.get(
                (ev.source_a, ev.key_a, ev.source_b, ev.key_b), ""
            )
        graph.add_confirmed_match(ev, proposer_reason=pr)
    graph.flag_conflicts()
    return graph


def rows_to_node_map(rows: list[MatchableRow]) -> dict[Node, MatchableRow]:
    return {(r.source, r.key): r for r in rows}


def _has_multi_source_conflict(
    node: Node,
    adjacency: dict[Node, set[Node]],
    neighbour_source: str,
) -> bool:
    """True if `node` has ≥2 neighbours from `neighbour_source`.

    Lybrate N-to-1 is explicitly excluded — multiple lybrate neighbours
    for a GP/Practo node is expected and not a conflict.
    """
    if neighbour_source == "lybrate":
        return False
    same_source_neighbours = [
        n for n in adjacency.get(node, set())
        if n[0] == neighbour_source
    ]
    return len(same_source_neighbours) > 1


__all__ = [
    "Node",
    "MatchEdge",
    "MatchGraph",
    "build_graph",
    "rows_to_node_map",
]
