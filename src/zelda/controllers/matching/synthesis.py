"""Stage 5 — Synthesis LLM + lead assembly.

For matched clusters (≥2 source rows), the Synthesis LLM (Sonnet) is given
all rows and all match evidence and produces a canonical lead record.

For standalone rows (not in any cluster), leads are assembled deterministically
from the single source row — no LLM call needed.

Field priority for deterministic merging (fallback when synthesis is skipped):
  name:             google_places > practo > lybrate
  address:          google_places > practo > lybrate
  phone:            lybrate > google_places  (lybrate has real numbers)
  website:          google_places > practo
  google_maps_url:  google_places only
  rating/count:     google_places only
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import anthropic
from loguru import logger

from zelda.controllers.matching.graph import MatchEdge, MatchGraph, Node
from zelda.controllers.matching.prompt_loader import render_prompt
from zelda.models.lead import Lead
from zelda.models.matchable_row import MatchableRow


SYNTHESIS_MODEL = "claude-sonnet-4-6"

_SYNTHESIS_TOOL: dict[str, Any] = {
    "name": "canonical_lead",
    "description": "Produce a single canonical lead record from multiple matched clinic listings.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Best clinic name across all sources.",
            },
            "address": {
                "type": "string",
                "description": "Best physical address. Omit if genuinely unknown.",
            },
            "phone": {
                "type": "string",
                "description": "Best phone number (prefer real direct numbers over tracking proxies).",
            },
            "website": {
                "type": "string",
                "description": "Best website URL.",
            },
            "notes": {
                "type": "string",
                "description": "Any caveats, conflicts, or ambiguities in the merge.",
            },
        },
        "required": ["name"],
    },
}


def _synthesis_prompt(rows: list[MatchableRow], edges: list[MatchEdge]) -> str:
    return render_prompt("matching/synthesis.j2", rows=rows, edges=edges)


class SynthesisEngine:
    """Assembles Lead objects from match graph clusters."""

    def __init__(
        self,
        client: anthropic.Anthropic,
        node_map: dict[Node, MatchableRow],
        graph: MatchGraph,
        *,
        city: str,
        run_id: str,
        synthesis_model: str = SYNTHESIS_MODEL,
    ) -> None:
        self._client = client
        self._node_map = node_map
        self._graph = graph
        self._city = city
        self._run_id = run_id
        self._model = synthesis_model

    def build_leads(self) -> list[Lead]:
        """Build one Lead per cluster + one Lead per unmatched row."""
        matched_nodes: set[Node] = set()
        leads: list[Lead] = []

        for cluster in self._graph.connected_components():
            matched_nodes.update(cluster)
            lead = self._lead_from_cluster(cluster)
            if lead:
                leads.append(lead)

        # Standalone rows — every row not in any matched cluster.
        all_nodes = set(self._node_map.keys())
        for node in all_nodes - matched_nodes:
            row = self._node_map[node]
            leads.append(self._lead_from_standalone(row))

        return leads

    def _lead_from_cluster(self, cluster: list[Node]) -> Lead | None:
        rows = [self._node_map[n] for n in cluster if n in self._node_map]
        if not rows:
            return None

        edges = self._graph.edges_for_cluster(cluster)
        human_review = any(e.human_review for e in edges)
        avg_confidence = (
            sum(e.confidence for e in edges) / len(edges) if edges else 0.0
        )

        # Call synthesis LLM for canonical field values.
        canonical = self._call_synthesis_llm(rows, edges)

        # Collect source attribution.
        gp_id = next((n[1] for n in cluster if n[0] == "google_places"), None)
        practo_url = next((n[1] for n in cluster if n[0] == "practo"), None)
        lybrate_urls = [n[1] for n in cluster if n[0] == "lybrate"]

        # Fall back to row data for fields synthesis didn't populate.
        gp_row = next((r for r in rows if r.source == "google_places"), None)
        ly_row = next((r for r in rows if r.source == "lybrate"), None)

        return Lead(
            lead_id=str(uuid.uuid4()),
            city=self._city,
            run_id=self._run_id,
            tier="enriched",
            name=canonical.get("name") or rows[0].name,
            address=canonical.get("address") or _first(rows, "address"),
            lat=gp_row.lat if gp_row else _first(rows, "lat"),
            lng=gp_row.lng if gp_row else _first(rows, "lng"),
            phone=canonical.get("phone") or (ly_row.phone if ly_row else None) or _first(rows, "phone"),
            website=canonical.get("website") or _first(rows, "website"),
            google_maps_url=gp_row.google_maps_url if gp_row else None,
            rating=gp_row.rating if gp_row else None,
            review_count=gp_row.review_count if gp_row else None,
            google_places_id=gp_id,
            practo_url=practo_url,
            lybrate_urls=lybrate_urls,
            match_confidence=avg_confidence,
            match_notes=canonical.get("notes"),
            human_review_needed=human_review,
            source_data={
                "cluster_size": len(rows),
                "sources": [r.source for r in rows],
            },
            created_at=datetime.now(timezone.utc),
        )

    def _lead_from_standalone(self, row: MatchableRow) -> Lead:
        return Lead(
            lead_id=str(uuid.uuid4()),
            city=self._city,
            run_id=self._run_id,
            tier="standalone",
            name=row.name,
            address=row.address,
            lat=row.lat,
            lng=row.lng,
            phone=row.phone,
            website=row.website,
            google_maps_url=row.google_maps_url,
            rating=row.rating,
            review_count=row.review_count,
            google_places_id=row.key if row.source == "google_places" else None,
            practo_url=row.key if row.source == "practo" else None,
            lybrate_urls=[row.key] if row.source == "lybrate" else [],
            source_data={"source": row.source},
            created_at=datetime.now(timezone.utc),
        )

    def _call_synthesis_llm(
        self,
        rows: list[MatchableRow],
        edges: list[MatchEdge],
    ) -> dict[str, Any]:
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=1024,
                tools=[_SYNTHESIS_TOOL],
                tool_choice={"type": "tool", "name": "canonical_lead"},
                messages=[{
                    "role": "user",
                    "content": _synthesis_prompt(rows, edges),
                }],
            )
            for block in response.content:
                if block.type == "tool_use":
                    return block.input  # type: ignore[return-value]
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "synthesis.llm_error cluster_size={n} err={e}",
                n=len(rows), e=e,
            )
        return {}


def _first(rows: list[MatchableRow], field: str) -> Any:
    """Return the first non-None value for `field` across rows."""
    for row in rows:
        val = getattr(row, field, None)
        if val is not None:
            return val
    return None


__all__ = [
    "SYNTHESIS_MODEL",
    "SynthesisEngine",
]
