"""Cross-source matching pipeline.

Stages:
  1. prefilter  — geo (1 km) + name-token filter → candidate pairs
  2. llm_judge  — Proposer LLM (Haiku) + Reviewer LLM (Sonnet) per pair
  3. graph      — match graph + conflict detection
  4. synthesis  — Synthesis LLM (Sonnet) → canonical lead per cluster
  5. pipeline   — orchestrates all stages, persists leads
"""
