# Zelda

AI growth platform for dental practices in India. Zelda discovers, deduplicates,
enriches, and scores independent dental clinics in a city — then surfaces the
highest-need leads to Drive for sales outreach.

## Running the pipeline for a new city

Four commands, run in order. Each is idempotent and resumable if interrupted.

```bash
# 1. Discover — pulls clinics from Google Places, Practo, and Lybrate
python -m zelda discover --city Bengaluru

# 2. Match — cross-source dedup → unified leads table (LLM-assisted)
python -m zelda match --city Bengaluru

# 3. Enrich — compute enrichment signals and score each lead
python -m zelda enrich-leads --city Bengaluru

# 4. Sync — push everything to Drive (discovery sheets + scored enrichment sheet)
python -m zelda sync --city Bengaluru
```

After step 4, Drive will contain:
- `{City}/discovery/google_places` — raw GP data
- `{City}/discovery/practo` — Practo listings
- `{City}/discovery/lybrate` — Lybrate listings
- `{City}/enrichment/leads` — scored leads, ordered by `need_score` DESC

**Known gaps (not blocking, but worth knowing):**

- **Review signals are a separate step.** Pass 1 enrichment (review velocity,
  revenue-leak keyword scan, negative-theme LLM classification) requires reviews
  to be fetched first via `python -m zelda fetch-reviews --city CITY`. This takes
  longer (Playwright) and is not yet integrated into the main 4-command flow.
  Without it, `review_velocity_*` and `has_revenue_leak_signal` will be zero/null.

- **LLM enrichment requires `ANTHROPIC_API_KEY`.** Pass 2 website audit classifies
  services and equipment via Haiku. If the key is missing, the audit still runs but
  `service_mix` and `equipment_claims` will be empty.

- **No single umbrella command yet.** A `zelda pipeline --city CITY` that chains
  all four steps will be added; for now, run them manually in sequence.

## Setup on a fresh machine

Prerequisite: Miniconda or Anaconda installed.

1. Clone this repo and `cd` into it.
2. Drop the OAuth client JSON at `secrets/oauth-client.json` (created in GCP Console → APIs & Services → Credentials → OAuth client ID → Desktop app). The OAuth token cache at `secrets/oauth-token.json` is created automatically on first auth — don't pre-create it.
3. Copy the env template and fill it in:
   ```
   cp .env.example .env
   # then edit .env and set GOOGLE_PLACES_API_KEY
   ```
4. Create the conda env (this also installs `zelda` editable):
   ```
   conda env create -f environment.yml
   conda activate zelda
   ```
5. Install the headless Chromium browser used for review scraping (~150 MB, one-time):
   ```
   playwright install chromium
   ```
6. Smoke-check the install:
   ```
   python -c "import zelda; print(zelda.__version__)"
   python -m pytest
   ```

   Note: always invoke pytest as `python -m pytest`, not bare `pytest` — if homebrew (or another package manager) installed a global `pytest` binary, it can shadow the conda env's version on PATH.

7. (Optional, on a fresh machine.) Pull existing Google Places leads from Drive into the empty local DB so you don't re-discover from scratch:
   ```
   python -m zelda bootstrap --city Ludhiana
   ```
   This downloads the JSONL artifacts from `raw-artifacts/{slug(city)}/` on Drive, reconstructs `GooglePlacesLead`s from them (lossless — JSONL contains the full Place Details payload), and upserts into local SQLite. Idempotent on re-run.

## Architecture: pipeline-of-phases

```
Phase 1 — Discovery (independent per-source steps)
  ├── google_places   →  google_places_leads        (Google Places API)
  ├── practo          →  practo_listings            (httpx + JSON-LD)
  └── lybrate         →  lybrate_listings           (httpx + schema.org)

Phase 2 — Matching (LLM-assisted cross-source dedup)
  └── Proposer + Reviewer LLM pair → leads          (unified table, one row per clinic)

Phase 3 — Enrichment (signal passes, each idempotent)
  ├── Pass 0  — existing DB data (free, instant)
  ├── Pass 1  — review history signals (ReviewRepo + Haiku LLM)  ← separate fetch-reviews step
  ├── Pass 2  — website audit (HTTP + Haiku LLM)
  ├── Pass 3  — Practo signals (practo_listings + practo_profiles)
  └── Pass 5  — lead scoring → need_score, score_tier, pitch_angle

Phase 4 — Sync (all tables → Drive)
  ├── discovery/google_places, discovery/practo, discovery/lybrate
  └── enrichment/leads                              (scored, ordered by need_score DESC)
```

Adding a new lead source = build its `Gateway + Controller + Step` triple
and register the step in the CLI's pipeline construction. The pipeline
orchestrator's loop logic doesn't change. Failures or blocks in one
source step do not abort sibling steps.

Each source persists into its **own** per-source table, with the source's
natural key as the primary key (Google `place_id`, Practo / Lybrate
profile URL). Cross-source linking is handled by the matching phase —
discovery never discards a lead just because another source didn't
surface it.

## Commands

```
python -m zelda discover --city CITY
                         [--sources google_places,practo,lybrate]
                         [--gp-max-results N|all] [--gp-max-pages N]
```
Runs the discovery pipeline for `CITY`. Default: all registered sources
(`google_places`, `practo`, `lybrate`). `--sources` filters to a subset;
unknown source names error out.

Per-source knobs are prefixed with the source name:
- `--gp-max-results` — Google Places only. Cost knob (Place Details is the expensive call). Default `1`. `0` = dry run, `all` = unlimited.
- `--gp-max-pages` — Google Places only. Pagination depth per text-search query. Default `1` (≈20 results).

Practo and Lybrate have no cost knobs — their directories are small (~80 / ~100 entries per Indian metro) and crawl cheaply over plain HTTPS in a few seconds.

```
python -m zelda sync --city CITY
                     [--sources google_places,practo,lybrate]
                     [--watch] [--interval-seconds 60]
```
Pushes all three per-source tables to Drive, one sheet per source. Only rows where `last_synced_at IS NULL OR last_modified_at > last_synced_at` are touched (delta-only). Marks synced **only after** a successful Drive write — if Drive is down, rows stay pending and the next run retries (at-least-once delivery).

Drive layout: `{root}/{City}/discovery/{source}` (Sheet). For Google Places, local JSONL artifacts are also mirrored to `{root}/{City}/discovery/raw-artifacts/`.

`--watch` keeps the pipeline running, re-syncing every `--interval-seconds` (default 60). This is the recommended mode for a live pipeline — discovery / enrichment just bump `last_modified_at`; the watcher picks up changes on its next tick without manual triggering. `--sources` restricts which step(s) run, same as `discover`.

```
python -m zelda bootstrap --city CITY
```
Reverse direction for Google Places leads only: Drive → local DB. Pulls JSONL artifacts from `raw-artifacts/{slug(city)}/` on Drive, reconstructs `GooglePlacesLead` rows from each Place Details payload, and upserts into local SQLite. Used on a fresh machine. Idempotent.

```
python -m zelda fetch-reviews --city CITY [--max-places N|all] [--max-reviews-per-place N]
```
Single-source command: captures Google Maps reviews per Google Places lead via the Playwright reviews gateway. Newest-first, with capture metadata (`is_truncated`, `total_per_gbp`, capture order, status) persisted alongside the reviews.

```
python -m zelda enrich --city CITY [--max-leads N|all] [--max-age-days N] [--force-refresh] [--sources google_reviews,practo_profile]
```
Runs the per-Google-Places-lead enrichment orchestrator (reviews + Practo profile fetch). Source-level caching: `--max-age-days` (default 180) controls the cache window. Practo profile enrichment requires that a `practo_profiles` URL stub already exists for a lead — discovery doesn't populate that table; cross-source matching (a future phase) will. Until then, `enrich --city ...` runs cleanly but the Practo source is effectively a no-op except on rows an operator manually populated.

### Example flow

```
python -m zelda discover --city Ludhiana
# discover Ludhiana: steps_ran=3 discovered=228 inserted=166 errors=0 …
#   [google_places] discovered=63 inserted=1 already_known=0 …
#   [practo]        discovered=68 inserted=68 already_known=0 …
#   [lybrate]       discovered=97 inserted=97 already_known=0 …

# Re-running is idempotent for Practo/Lybrate; Google Places only
# inserts up to --gp-max-results new rows per run.
python -m zelda discover --city Ludhiana
#   [practo]  inserted=0 already_known=68
#   [lybrate] inserted=0 already_known=97

# Practo+Lybrate-only run (no Google Places API spend):
python -m zelda discover --city Ludhiana --sources practo,lybrate
```

Once you've validated end-to-end with low caps, scale Google Places up with `--gp-max-results all`.

```
# Push all three sources to Drive (one-shot):
python -m zelda sync --city Ludhiana
# sync Ludhiana: run_id=sync-… pulled=228 inserted=228 updated=0 errors=0 aborted=False
#   [google_places] pulled=63 inserted=63 updated=0 artifacts_uploaded=2 artifacts_skipped=0 …
#   [practo]        pulled=68 inserted=68 updated=0 …
#   [lybrate]       pulled=97 inserted=97 updated=0 …

# Keep Drive live as discovery/enrichment runs:
python -m zelda sync --city Ludhiana --watch --interval-seconds 60
```

## Layout

```
src/zelda/
  models/                                  # Pydantic data shapes
    google_places_lead.py
    practo_listing.py
    lybrate_listing.py
    practo_profile.py                      # enrichment (Playwright fetch)
    review.py                              # enrichment (Maps reviews)
    place.py
  gateways/                                # external API wrappers
    google_places.py
    google_drive.py
    google_reviews.py                      # Playwright (Maps reviews)
    practo_directory.py                    # httpx (Practo per-city directory)
    lybrate_directory.py                   # httpx (Lybrate per-city directory)
    practo_playwright.py                   # Playwright (Practo profile pages)
    _practo_browser.py                     # shared Playwright shim
  repositories/                            # SQLite persistence
    google_places_lead_repo.py
    practo_listing_repo.py
    lybrate_listing_repo.py
    practo_profile_repo.py                 # enrichment
    review_repo.py                         # enrichment
  controllers/                             # use-case orchestration
    discover.py                            # Google Places discovery
    practo_directory.py                    # Practo discovery
    lybrate_directory.py                   # Lybrate discovery
    discovery_pipeline.py                  # DiscoveryStep Protocol + Pipeline
    discovery_steps.py                     # concrete steps wrapping the above
    bootstrap.py                           # Drive → DB
    sync_pipeline.py                       # SyncStep Protocol + SyncPipeline
    sync_steps.py                          # concrete steps (GP, Practo, Lybrate)
    fetch_reviews.py                       # enrichment
    enrich_practo.py                       # enrichment
    enrichment_orchestrator.py             # enrichment
    enrichment_sources.py                  # enrichment SourceAdapter Protocol + adapters
  config.py                                # pydantic-settings (.env)
  cli.py                                   # argparse subcommands
  __main__.py                              # `python -m zelda`
  util.py

tests/                                     # 414+ unit tests (in-memory + mocked)
  fixtures/                                # cached HTML / API payloads
scripts/                                   # smoke tests vs. real services
data/                                      # local SQLite + JSONL artifacts (gitignored)
secrets/                                   # OAuth client + token cache (gitignored)
```

Architecture rules:
- **Layering**: gateway → controller → repository, with models as the data shape between layers. Controllers never talk to gateways' transports directly; gateways never persist.
- **One source = one table.** No "leads" superset table; each source's data lives in its own typed shape until cross-source matching merges them.
- **Drive is a projection.** Discovery never writes to Drive directly; `sync` is the only writer.

## Auth model

- **Google Places API**: API key, set in `.env`. Personal-account or Workspace, doesn't matter — Places only requires billing on the GCP project.
- **Practo & Lybrate**: no auth — public listing pages, accessed via plain HTTPS with a real-browser User-Agent (Practo's listings are gated by Akamai for scripted UAs).
- **Google Drive + Sheets**: OAuth user credentials. The script acts as the user; new files end up in your personal Drive (charged against your free 15 GB quota). First run pops a browser tab for the OAuth consent screen; subsequent runs use the cached refresh token at `secrets/oauth-token.json`.

A previous iteration used a service account — service accounts have 0 GB of personal Drive storage, so they can only create files in Workspace Shared Drives. We may revisit if/when we move to a custom domain on Workspace.

## Drive folder structure

Inside the configured `GOOGLE_DRIVE_FOLDER_ID`:

```
Zelva/                                            ← root
└── Ludhiana/                                     (one per city)
    ├── discovery/
    │   ├── google_places                         (Sheet — 27 cols, lossless)
    │   ├── practo                                (Sheet — 10 cols)
    │   ├── lybrate                               (Sheet — 15 cols)
    │   └── raw-artifacts/                        (Google Places JSONL dumps)
    │       ├── 20260429-125652-ad4e.jsonl
    │       └── …
    └── enrichment/
        └── leads                                 (Sheet — 44 cols, ordered by need_score DESC)
```

All JSON-typed columns (`reviews`, `types`, `address_components`, `raw_json`,
etc.) are serialized as JSON strings in the sheet — the Drive gateway
handles this automatically. Nothing is dropped.
