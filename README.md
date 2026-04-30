# Zelda

AI growth platform for dental practices in India. V1 is a lead-generation pipeline that takes a city as input, discovers dentists via Google Places, persists them in SQLite, and mirrors them to a Google Drive sheet for review.

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

7. (Optional, on a fresh machine.) Pull existing leads from Drive into the empty local DB so you don't re-discover from scratch:
   ```
   python -m zelda bootstrap --city Ludhiana
   ```
   This downloads the JSONL artifacts from `raw-artifacts/{slug(city)}/` on Drive, reconstructs RawLeads from them (lossless — JSONL contains the full Place Details payload), and upserts into local SQLite. Idempotent on re-run.

## Commands

```
python -m zelda discover --city CITY [--max-results N|all] [--max-pages N]
```
Runs Google Places Text Search across 7 dental queries for the given city, dedupes by `place_id`, fetches Place Details for ones not already in the DB, and persists everything to local SQLite + a JSONL artifact under `data/raw-artifacts/{slug(city)}/`.

- `--max-results` is the cost knob (Place Details is the expensive call). Default `1` — explicit opt-in to bigger runs. `0` = dry run (no fetches). `all` = unlimited.
- `--max-pages` is pagination depth per text-search query. Default `1` (≈20 results per query).

```
python -m zelda sync --city CITY
```
Pushes any rows in SQLite where `last_synced_at IS NULL OR last_modified_at > last_synced_at` to a Google Sheet named `Zelda — Raw Leads — Dentists — {City}` under the configured Drive folder, then mirrors any local JSONL artifacts to `raw-artifacts/{slug(city)}/` on Drive. Idempotent: re-running with no DB changes is a no-op.

```
python -m zelda bootstrap --city CITY
```
Reverse direction: Drive → local DB. Pulls JSONL artifacts from `raw-artifacts/{slug(city)}/` on Drive, reconstructs RawLead rows from each Place Details payload, and upserts into the local SQLite. Used on a fresh machine to inherit existing discovered leads without re-paying the Places API. Idempotent: skips JSONLs already on disk, repo upserts are no-ops if data is unchanged. After bootstrap, leads are marked synced — a follow-up `sync` call is also a no-op.

```
python -m zelda fetch-reviews --city CITY [--max-places N|all] [--max-reviews-per-place N]
```
Single-source command: captures Google Maps reviews per place via the Playwright reviews gateway. Newest-first, with capture metadata (`is_truncated`, `total_per_gbp`, capture order, status) persisted alongside the reviews so downstream stat functions can't lie about bounds. Useful for one-off review-only refreshes without invoking other enrichment sources.

```
python -m zelda enrich --city CITY [--max-leads N|all] [--max-age-days N] [--force-refresh] [--sources google_reviews,practo_profile]
```
**The unified enrichment pipeline.** Iterates over (lead × source) pairs and only fetches what's missing or stale. Each source ([`enrichment_sources.py`](src/zelda/controllers/enrichment_sources.py)) implements three predicates: `can_fetch` (do we have what we need to fetch this lead?), `is_cached_fresh` (recent successful capture exists?), `fetch_for_lead`. The orchestrator skips a fetch on cache-hit AND on no-prerequisite-data, and disables a single source for the rest of the run if it gets blocked — without affecting other sources.

- `--max-age-days` is the cache window. Default **180 (6 months)**. Source-level: a recent successful Practo capture skips Practo for that lead; same for reviews independently.
- `--force-refresh` bypasses the cache.
- `--sources` filters to a subset; default = all registered.
- Adding a new enrichment source = write a `SourceAdapter` (~50 lines) and register it in the CLI handler. The orchestrator's loop logic doesn't change.

### Example flow

```
python -m zelda discover --city Ludhiana --max-results 1
# discover Ludhiana: deduped=60 new_eligible=58 ... fetched=1 inserted=1 ...

python -m zelda sync --city Ludhiana
# sync Ludhiana: unsynced=1 sheet_inserted=1 ... artifacts_uploaded=1 ...

python -m zelda enrich --city Ludhiana --max-leads 1
# enrich Ludhiana: leads=59 after_max_leads=1 ...
#   [google_reviews] attempted=1 successful=1 ...
#   [practo_profile] no_prereq=1 attempted=0 ...
```

Once you've validated end-to-end with low caps, scale up with `--max-results all` / `--max-leads all`.

## Layout

```
src/zelda/
  models/         pure data shapes (Pydantic)
  gateways/       wrappers around external APIs (Google Places, Google Drive)
  repositories/   persistence layer (SQLite)
  controllers/    use-case orchestration (discover, sync)
  config.py       env-driven settings
  cli.py          argparse subcommands
  __main__.py     enables `python -m zelda`
  util.py         small shared helpers (slugify)

tests/            pytest suite (160+ unit tests, in-memory + mocked)
scripts/          manual smoke tests against real APIs
data/             local SQLite + raw artifact JSONL (gitignored)
secrets/          OAuth client + token cache (gitignored)
```

Architecture: gateway / repository / controller / model layers. Drive is a one-way projection of SQLite — discovery never writes to Drive directly. Sync runs on its own cadence and can be triggered manually, by cron, or as a `--watch` daemon (not built yet).

## Auth model

- **Google Places API**: API key, set in `.env`. Personal-account or Workspace, doesn't matter — Places only requires billing on the GCP project.
- **Google Drive + Sheets**: OAuth user credentials. The script acts as the user; new files end up in your personal Drive (charged against your free 15 GB quota). First run pops a browser tab for the OAuth consent screen; subsequent runs use the cached refresh token at `secrets/oauth-token.json`.

A previous iteration used a service account — service accounts have 0 GB of personal Drive storage, so they can only create files in Workspace Shared Drives. We may revisit if/when we move to a custom domain on Workspace.

## Drive folder structure

Inside the configured `GOOGLE_DRIVE_FOLDER_ID`:

```
Zelva/                                            ← root
├── Zelda — Raw Leads — Dentists — Ludhiana      (Sheet, one per city)
├── Zelda — Raw Leads — Dentists — Mumbai        (Sheet, future)
└── raw-artifacts/                               (audit JSONL dumps)
    └── ludhiana/
        ├── 20260429-125652-ad4e.jsonl
        └── …
```
