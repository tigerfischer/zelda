# Zelda

AI growth platform for dental practices in India. V1 is a lead-generation pipeline that takes a city as input and produces a ranked list of candidate clinics, mirrored to a Google Drive sheet for review.

## Setup on a fresh machine

Prerequisite: Miniconda or Anaconda installed.

1. Clone this repo and `cd` into it.
2. Drop the Google service account JSON at `secrets/service-account.json`.
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
5. Smoke-check the install:
   ```
   python -c "import zelda; print(zelda.__version__)"
   python -m pytest --collect-only
   ```

   Note: always invoke pytest as `python -m pytest`, not bare `pytest` —
   if homebrew (or another package manager) installed a global `pytest`
   binary, it can shadow the conda env's version on PATH.

## Layout

```
src/zelda/
  models/         pure data shapes (Pydantic)
  gateways/       wrappers around external APIs (Google Places, Google Drive)
  repositories/   persistence layer (SQLite)
  controllers/    use-case orchestration (discover, drive sync, monitor)
  config.py       env-driven settings
  cli.py          entry points

tests/            pytest suite
data/             local SQLite + raw artifact JSONL (gitignored)
secrets/          service account keys (gitignored)
```

Architecture: gateway / repository / controller / model layers. Drive is a one-way projection of SQLite; discovery never writes to Drive directly.

## Commands

(filled in as built — see phase 7.)
