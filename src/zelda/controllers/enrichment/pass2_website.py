"""Pass 2 — website audit signals.

Fetches the clinic's website (from `lead.website` or GP data) using
`WebsiteAuditGateway` (plain HTTP + BeautifulSoup, no Playwright) and
extracts:

  - website_loads              — HTTP 200 received
  - website_is_mobile_friendly — viewport <meta> present
  - website_has_schema_markup  — JSON-LD with Dentist / LocalBusiness
  - website_has_blog           — /blog or /articles links
  - has_whatsapp_link          — wa.me/ or WhatsApp API link anywhere
  - has_online_booking         — Calendly / SimplyBook / booking CTA
  - has_chat_widget            — Tidio / Intercom / Tawk / similar
  - website_agency_credit      — "designed by <agency>" in footer
  - service_mix                — LLM (Haiku) classification of visible text
  - equipment_claims           — equipment keywords found in page text

Skipped if `lead.website` is None and GP data has no website — the
`website_loads` signal is left as None (not False) to distinguish
"no website found" from "website found but doesn't load".

LLM call: one Haiku call per lead that has a loadable website.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import anthropic
from loguru import logger

from zelda.controllers.matching.prompt_loader import render_prompt
from zelda.gateways.website_audit import WebsiteAuditGateway
from zelda.models.lead import Lead
from zelda.models.lead_enrichment import LeadEnrichment
from zelda.repositories.google_places_lead_repo import GooglePlacesLeadRepository

_PASS_NAME = "pass2"

# ── Service-mix LLM tool ───────────────────────────────────────────────

_SERVICE_TOOL: dict[str, Any] = {
    "name": "service_classification",
    "description": "Classify a dental clinic's service offering and equipment.",
    "input_schema": {
        "type": "object",
        "properties": {
            "services": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "general", "implants", "orthodontics", "cosmetic",
                        "paediatric", "root_canal", "oral_surgery",
                        "gum_treatment", "teeth_whitening", "veneers",
                        "dentures", "emergency",
                    ],
                },
                "description": "Service categories explicitly mentioned in the text.",
            },
            "equipment": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["cbct", "opg", "digital_xray", "laser", "intraoral_scanner"],
                },
                "description": "Specialised equipment explicitly mentioned.",
            },
        },
        "required": ["services", "equipment"],
    },
}

# ── Equipment keyword scan (fast fallback, no LLM) ────────────────────

_EQUIPMENT_PATTERNS: dict[str, re.Pattern[str]] = {
    "cbct": re.compile(r"\bcbct\b|\bcone\s*beam\b", re.IGNORECASE),
    "opg": re.compile(r"\bopg\b|\borthopantomogram\b|\bpanoramic\s*x.?ray\b", re.IGNORECASE),
    "digital_xray": re.compile(r"\bdigital\s*x.?ray\b|\bRVG\b|\bradiovisiography\b", re.IGNORECASE),
    "laser": re.compile(r"\blaser\s*dent|\bdental\s*laser\b|\bdiode\s*laser\b", re.IGNORECASE),
    "intraoral_scanner": re.compile(r"\bintraoral\s*scan|\b3d\s*scan|\bdigital\s*impression\b", re.IGNORECASE),
}


def run(
    lead: Lead,
    enrichment: LeadEnrichment,
    *,
    gp_repo: GooglePlacesLeadRepository,
    gateway: WebsiteAuditGateway,
    anthropic_client: anthropic.Anthropic | None = None,
    llm_model: str = "claude-haiku-4-5-20251001",
) -> LeadEnrichment:
    """Compute Pass 2 signals from the clinic website."""
    now = datetime.now(timezone.utc)

    # Resolve website URL — lead.website is already the merged best-available value
    url = lead.website
    if not url and lead.google_places_id:
        gp = gp_repo.get_by_id(lead.google_places_id)
        if gp:
            url = gp.website

    if not url:
        # No website — leave website_loads as None (distinct from False)
        enrichment.passes_completed[_PASS_NAME] = now.isoformat()
        enrichment.updated_at = now
        return enrichment

    # ── Fetch and parse ─────────────────────────────────────────────────
    audit = gateway.audit(url)

    enrichment.website_loads = audit.get("website_loads", False)
    if not enrichment.website_loads:
        logger.debug(
            "enrichment.pass2 lead_id={lid} website_loads=False err={e}",
            lid=lead.lead_id, e=audit.get("error"),
        )
        enrichment.passes_completed[_PASS_NAME] = now.isoformat()
        enrichment.updated_at = now
        return enrichment

    enrichment.website_is_mobile_friendly = audit.get("is_mobile_friendly")
    enrichment.website_has_schema_markup = audit.get("has_schema_markup")
    enrichment.website_has_blog = audit.get("has_blog")
    enrichment.has_whatsapp_link = audit.get("has_whatsapp_link")
    enrichment.has_online_booking = audit.get("has_online_booking")
    enrichment.has_chat_widget = audit.get("has_chat_widget")
    enrichment.website_agency_credit = audit.get("agency_credit")

    page_text = audit.get("page_text", "")

    # ── Equipment keyword scan (always run, cheap) ─────────────────────
    found_equipment = [
        equip for equip, pattern in _EQUIPMENT_PATTERNS.items()
        if pattern.search(page_text)
    ]
    if found_equipment:
        enrichment.equipment_claims = found_equipment

    # ── Service-mix LLM classification ─────────────────────────────────
    if page_text and anthropic_client is not None:
        services, equipment_llm = _classify_services(
            page_text, lead.name, anthropic_client, llm_model
        )
        enrichment.service_mix = services
        # Merge LLM equipment with keyword-scan findings
        merged = list(set(found_equipment) | set(equipment_llm))
        if merged:
            enrichment.equipment_claims = merged

    enrichment.passes_completed[_PASS_NAME] = now.isoformat()
    enrichment.updated_at = now

    logger.debug(
        "enrichment.pass2 lead_id={lid} loads={l} mobile={m} "
        "booking={b} whatsapp={wa} services={s}",
        lid=lead.lead_id,
        l=enrichment.website_loads,
        m=enrichment.website_is_mobile_friendly,
        b=enrichment.has_online_booking,
        wa=enrichment.has_whatsapp_link,
        s=enrichment.service_mix,
    )
    return enrichment


# ── helpers ────────────────────────────────────────────────────────────

def _classify_services(
    page_text: str,
    clinic_name: str,
    client: anthropic.Anthropic,
    model: str,
) -> tuple[list[str], list[str]]:
    """Returns (services, equipment) from LLM classification."""
    try:
        prompt = render_prompt(
            "enrichment/service_mix.j2",
            clinic_name=clinic_name,
            page_text=page_text,
        )
        response = client.messages.create(
            model=model,
            max_tokens=256,
            tools=[_SERVICE_TOOL],
            tool_choice={"type": "tool", "name": "service_classification"},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in response.content:
            if block.type == "tool_use":
                inp = block.input  # type: ignore[union-attr]
                return inp.get("services", []), inp.get("equipment", [])
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "enrichment.pass2.llm_error clinic={n} err={e}", n=clinic_name, e=e
        )
    return [], []


__all__ = ["run"]
