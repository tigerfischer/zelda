"""Lead enrichment pipeline — five passes that compute signals from
all available data sources and produce a scored `LeadEnrichment` per lead.

Passes:
  0 — Existing DB data   (free, instant)
  1 — Full review history (ReviewRepository + LLM Haiku)
  2 — Website audit       (HTTP + BeautifulSoup + LLM Haiku)
  3 — Practo signals      (practo_listings + practo_profiles)
  5 — Lead scoring        (pure computation, no I/O)

Pass 4 (photo vision) is reserved for future implementation.
"""
