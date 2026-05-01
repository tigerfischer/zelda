# New City Onboarding Runbook

End-to-end guide for taking a city from zero to outreach-ready.
Typical total time: **3–5 hours** (mostly waiting on fetch-reviews).

---

## Prerequisites

- [ ] `.env` file set with `GOOGLE_PLACES_API_KEY`, `GOOGLE_DRIVE_FOLDER_ID`, `ANTHROPIC_API_KEY`
- [ ] `conda activate zelda` (always run commands in this env)
- [ ] Drive OAuth token cached (`secrets/oauth-token.json` exists — if not, first `sync` run will open a browser)
- [ ] Internet connection (no VPN — Playwright needs a clean residential-ish IP)

---

## Phase 1 — Discovery (~5–10 min)

Pulls clinics from all three sources into their own tables.

```bash
python -m zelda discover --city "City Name"
```

Expected output:
```
[google_places] discovered=63  inserted=63  already_known=0
[practo]        discovered=68  inserted=68  already_known=0
[lybrate]       discovered=97  inserted=97  already_known=0
```

**Knobs:**
- `--gp-max-results all` — fetch all Google Places results (default is 1, safe for first run)
- `--sources practo,lybrate` — skip Google Places API spend and just crawl free sources

**Idempotent** — safe to re-run. Practo and Lybrate will show `inserted=0, already_known=N` on re-run.

---

## Phase 2 — Matching (~2–5 min, uses Claude Haiku)

Cross-source dedup → unified `leads` table. One row per real-world clinic.

```bash
python -m zelda match --city "City Name"
```

**Requires:** `ANTHROPIC_API_KEY` in `.env`

Expected: "269 leads, 166 inserted, 103 already matched" (numbers vary by city).

**If match fails with API error:** check `ANTHROPIC_API_KEY` is set and valid.

---

## Phase 3 — First Enrichment (passes 0, 2, 3, 5) (~20–60 min)

Computes enrichment signals and scores every lead. Pass 1 (reviews) is skipped here — done after fetch-reviews.

```bash
python -m zelda enrich-leads --city "City Name"
```

**What each pass does:**
| Pass | Source | Signals |
|---|---|---|
| 0 | Existing DB | review count/rating, GBP completeness, website flag |
| 2 | Website audit (HTTP + Claude) | loads, mobile-friendly, booking, service mix |
| 3 | Practo/Lybrate listings | Practo rating, booking enabled, consultation fee |
| 5 | Scoring | `need_score`, `score_tier`, `pitch_angle` |

**Skip pass 1 for now** — it reads from the `reviews` table which is empty until fetch-reviews runs.

---

## Phase 4 — Fetch Reviews (~2–4 hours, Playwright)

Scrapes Google Maps reviews for every Google Places lead. This is the slow step — it throttles deliberately to look human.

```bash
python -m zelda fetch-reviews --city "City Name" --max-places all --max-reviews-per-place 200
```

**Run this in a dedicated terminal tab and leave it.** Progress is visible:
```bash
# In another tab, while fetch-reviews runs:
cat data/progress/fetch-reviews-<city-slug>.json
```

**If interrupted:** just re-run — it skips places already captured within 7 days (configurable with `--refresh-min-age-days`).

**If blocked (CAPTCHA):** the run aborts automatically. Wait 30–60 minutes and re-run. The already-captured leads are safe.

---

## Phase 5 — Re-enrich with Review Signals + Re-score (~10 min)

Now that reviews exist, run pass 1 (review velocity, owner response rate, revenue leak signals) and re-run pass 5 (scoring) so the scores reflect the review data.

```bash
# Pass 1 — review signals
python -m zelda enrich-leads --city "City Name" --passes 1

# Pass 5 — re-score with the new signals
python -m zelda enrich-leads --city "City Name" --passes 5 --force
```

**`--force` on pass 5** is needed because pass 5 already ran in Phase 3 — without `--force`, it skips leads it's already processed.

---

## Phase 6 — Sync to Drive (~2–5 min)

Pushes all four sheets to Drive. Creates the city folder structure automatically.

```bash
python -m zelda sync --city "City Name"
```

Drive will now contain:
```
Zelva/
└── City Name/
    ├── discovery/
    │   ├── google_places    (sheet)
    │   ├── practo           (sheet)
    │   ├── lybrate          (sheet)
    │   └── raw-artifacts/   (JSONL dumps)
    └── enrichment/
        └── leads            (sheet, ordered by need_score DESC)
```

**Verify in Drive:** open `enrichment/leads` and check that `need_score`, `pitch_angle`, and `clinic_name` columns are populated.

---

## Phase 7 — Generate Outreach Messages (~5–10 min, uses Claude Haiku)

Runs the WhatsApp personalization agent for every lead.

```bash
python -m zelda generate-outreach --city "City Name"
```

Output: `data/outreach/<city>/messages_<run_id>.jsonl`

**Before running:** review a sample of leads in the Drive `enrichment/leads` sheet to sanity-check the data quality. Look at need_score distribution — if most leads have need_score < 20, something may be off in enrichment.

---

## Phase 8 — Load into Telegram Queue

```bash
python -m zelda load-outreach --file data/outreach/<city>/messages_<run_id>.jsonl
```

This loads all messages into the outreach DB with `status=pending_review`. No messages are sent yet.

---

## Phase 9 — Start the Telegram Bot (if not already running)

```bash
python -m zelda telegram-bot
```

**Requires:** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GREEN_API_INSTANCE_ID`, `GREEN_API_TOKEN` in `.env`.
See `docs/outreach/whatsapp_setup_checklist.md` for setup instructions.

The bot will immediately push all pending drafts to your Telegram for review.

---

## Phase 10 — Create Call Recordings Folder in Drive

The `call-recordings/` root folder is already created. For a new city, create the city subfolder:

```bash
conda run -n zelda python -c "
from dotenv import load_dotenv; load_dotenv()
from zelda.config import Settings
from zelda.gateways.google_drive import GoogleDriveGateway

s = Settings()
drive = GoogleDriveGateway.from_oauth_file(s.google_oauth_client_secrets, s.google_oauth_token_cache, s.google_drive_folder_id)
root = drive.find_or_create_subfolder('call-recordings')
city_id = drive.find_or_create_subfolder('City Name', parent_folder_id=root)
print('City folder ready:', city_id)
"
```

Drop recordings into: `call-recordings/City Name/{lead_id}--{clinic-slug}/recording.m4a`

---

## Full Command Sequence (copy-paste)

```bash
CITY="City Name"

python -m zelda discover --city "$CITY" --gp-max-results all
python -m zelda match --city "$CITY"
python -m zelda enrich-leads --city "$CITY"
python -m zelda fetch-reviews --city "$CITY" --max-places all --max-reviews-per-place 200
# ↑ wait for this to finish (2–4 hours) ↑
python -m zelda enrich-leads --city "$CITY" --passes 1
python -m zelda enrich-leads --city "$CITY" --passes 5 --force
python -m zelda sync --city "$CITY"
python -m zelda generate-outreach --city "$CITY"
python -m zelda load-outreach --file data/outreach/$(echo $CITY | tr '[:upper:]' '[:lower:]' | tr ' ' '_')/messages_*.jsonl
# Start bot if not running:
python -m zelda telegram-bot
```

---

## Known Gaps (not blocking, but worth knowing)

| Gap | Impact | Workaround |
|---|---|---|
| Pass 1 (reviews) requires fetch-reviews first | `review_velocity_*` and `has_revenue_leak_signal` stay null until Phase 4+5 done | Always run Phases 4+5 before generating outreach |
| Pass 3 Practo signals need `practo_profiles` table | `practo_review_count`, `practo_rating`, `practo_booking_enabled` stay null | Will improve once cross-source matching populates profiles |
| Pass 2 website audit needs `ANTHROPIC_API_KEY` | `service_mix` and `equipment_claims` stay empty without it | Set the key; Haiku calls are < ₹1 per city |
| No single "pipeline" command yet | Must run phases manually | `zelda pipeline --city CITY` is planned |
| fetch-reviews only covers Google Places leads | Reviews for Practo-only / Lybrate-only clinics are not fetched | Acceptable — GP leads are the highest quality |

---

## Fresh Machine Setup

If running on a new machine (not the one with the existing DB):

```bash
# 1. Clone repo, set up conda env, install chromium
conda env create -f environment.yml
conda activate zelda
playwright install chromium

# 2. Set up .env and secrets/oauth-client.json

# 3. Pull existing data from Drive (avoids re-discovering from scratch)
python -m zelda bootstrap --city "City Name"

# 4. Continue from Phase 2 (matching) above
```
