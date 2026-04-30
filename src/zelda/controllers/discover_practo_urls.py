"""Practo URL-discovery controller — for each lead, searches Practo
and creates a stub row when a candidate matches above the threshold.

Where this fits
---------------
There are two phases of work for Practo enrichment:

1. **URL acquisition** — find each lead's Practo profile URL (this
   controller).
2. **Profile enrichment** — fetch the profile at that URL and parse
   the data (`EnrichPractoController`).

Until now the operator did (1) by hand: `repo.upsert_stub(place_id,
url)` for each lead. That doesn't scale to a city-wide enrichment
pass. This controller automates it by searching Practo's SERP and
fuzzy-matching candidates against the lead's clinic name.

Match policy
------------
- Skip leads that already have *any* row in `practo_profiles`
  (pending, ok, not_found, no_url_found, blocked, error). Idempotent
  on URL acquisition only — the enrichment controller's retry
  policy is its own concern. To re-discover a lead, an operator
  deletes its `practo_profiles` row.
- Search Practo for the lead's clinic name in the lead's city.
- Score every candidate against the lead's name with a
  rapidfuzz-based metric that combines `partial_ratio`
  (substring tolerance) and `token_set_ratio` (word-order +
  extra-word tolerance). The best candidate's max score gates
  the match decision.
- If the best score >= `min_match_score` (default 0.7): upsert a
  stub row with the candidate's URL.
- Otherwise: upsert a `no_url_found` sentinel row so the next pass
  doesn't re-search this lead.

Rate-limit posture
------------------
Same as `EnrichPractoController`: 4 s base + 0–3 s jitter between
gateway calls (configurable). We only sleep AFTER a real search —
skipped (already-known) leads don't trigger a pause. Halts the loop
on Akamai block.

Single-lead use
---------------
Pass `[lead]` to `discover_for_leads(...)` for the per-lead form
that the orchestrator can call before invoking the enrichment
controller.
"""

from __future__ import annotations

import random
import re
import time as _time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Protocol

from loguru import logger
from rapidfuzz import fuzz

from zelda.gateways.practo_search import (
    PractoSearchOutcome,
    PractoSearchResult,
)
from zelda.models.practo_profile import PractoProfile
from zelda.models.raw_lead import RawLead
from zelda.repositories.practo_profile_repo import PractoProfileRepository


# ── city slug normalization ────────────────────────────────────────


# Practo's path-segment city slugs differ from the Places API's city
# names in a handful of merged-metro / renamed-city cases. Keep this
# dict small: anything not listed falls through to `lowercase + spaces
# → hyphens`. Source: cross-checking google_places city names against
# https://www.practo.com/<city>/dentist URLs.
_CITY_OVERRIDES: dict[str, str] = {
    "bengaluru": "bangalore",
    "new delhi": "delhi",
    "calcutta": "kolkata",
    "bombay": "mumbai",
    "madras": "chennai",
    "gurugram": "gurgaon",
}


def practo_city_slug(city: str) -> str:
    """Normalize a Places-API city name to Practo's URL slug.

    Lowercase, collapse whitespace, then look up in the override
    dict; fall back to spaces→hyphens for unlisted cities.
    """
    if not city:
        return ""
    norm = " ".join(city.strip().lower().split())
    if not norm:
        return ""
    return _CITY_OVERRIDES.get(norm, norm.replace(" ", "-"))


# ── name normalization for fuzzy matching ──────────────────────────


# Salutations stripped from the start of a name. Listed longest-first
# so the loop prefers e.g. "doctor's" over "dr". Match is whole-token,
# possibly followed by punctuation.
_SALUTATIONS: tuple[str, ...] = (
    "doctor's",
    "doctors",
    "doctor",
    "dr's",
    "drs",
    "dr.",
    "dr",
)

# Suffixes that add noise without affecting clinic identity. After
# stripping, a Google name like "Sai Dental Clinic" reduces to "sai",
# which is short enough to overlap with "Dr. Sai Kumar" on Practo via
# `partial_ratio`. Listed longest-first so multi-word suffixes win.
#
# We strip iteratively (loop until no match), so e.g.
# "Saggar Dental Care Implant OPG CBCT Centre" peels:
#   centre → opg cbct → cbct → opg → implant → care → dental → "saggar".
_CLINIC_SUFFIXES: tuple[str, ...] = (
    "and implant centre",
    "and implant center",
    "and implant",
    "multi speciality dental",
    "multi speciality",
    "multispeciality",
    "multi specialty",
    "multispecialty",
    "implant centre",
    "implant center",
    "implant",
    "dental hospital",
    "dental clinic",
    "dental centre",
    "dental center",
    "dental care",
    "dental",
    "hospital",
    "clinic",
    "centre",
    "center",
    "cbct",
    "opg",
    "rvg",
    "care",
)


def normalize_name(name: str) -> str:
    """Aggressive normalization for fuzzy matching.

    Order of operations:
    1. NFKD-decompose unicode then drop combining marks (collapses
       math-bold variants, accents, etc.).
    2. Lowercase.
    3. Truncate at the first " - " separator. Google Places clinic
       names commonly include an SEO tail like "Sai Dental Clinic
       - Best Dentist Near Me in Ludhiana"; we drop the tail because
       it dilutes fuzzy matching and (worse) introduces false
       positives where the city name in the tail matches a
       differently-named candidate's clinic.
    4. Strip leading salutation ("Dr.", "Doctor's", ...).
    5. Drop punctuation.
    6. Iteratively strip trailing clinic-noise suffixes ("Dental
       Clinic", "Implant Centre", "OPG", "CBCT", ...). Iteration
       handles names with multiple stacked suffixes, e.g.
       "Saggar Dental Care Implant OPG CBCT Centre" → "saggar".
    7. Collapse whitespace.

    Returns the empty string for empty / whitespace-only input.
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()

    # Truncate at SEO-tail separator. Common ones: " - ", " | ", " • ".
    for sep in (" - ", " | ", " • ", " — "):
        i = s.find(sep)
        if i != -1:
            s = s[:i]
            break

    # Strip leading salutation.
    for sal in _SALUTATIONS:
        if s == sal:
            s = ""
            break
        if s.startswith(sal + " ") or s.startswith(sal + "."):
            s = s[len(sal):].lstrip(". ")
            break

    # Drop punctuation; collapse whitespace BEFORE suffix stripping so
    # "Dental Clinic," matches the suffix table.
    s = re.sub(r"[^\w\s]", " ", s)
    s = " ".join(s.split())

    # Iteratively strip trailing clinic-noise suffixes. Match either as
    # a separate trailing word or as the entire remaining string. Loop
    # until no suffix matches so stacked suffixes all peel off.
    while True:
        prev = s
        for suf in _CLINIC_SUFFIXES:
            if s == suf:
                s = ""
                break
            if s.endswith(" " + suf):
                s = s[: -len(suf)].rstrip()
                break
        if s == prev:
            break

    return s


def score_candidate(
    lead_name: str,
    candidate: PractoSearchResult,
) -> float:
    """Score a Practo SERP candidate against a lead's clinic name.

    Returns 0.0–1.0. Uses asymmetric scoring across the two candidate
    fields, calibrated against live Ludhiana data:

    - **`clinic_name` (lenient)**: `partial_ratio` and `token_set_ratio`,
      both substring/word-order-tolerant. Both sides are clinic names
      so substring overlap is meaningful (e.g. lead "Smile Care"
      matching candidate clinic "Care Smile Dentistry").
    - **`doctor_name` (strict)**: `ratio` and `token_sort_ratio`,
      both length-aware. Prevents common-surname false positives —
      a lead "Singh Dental Clinic" would otherwise score 100 against
      every doctor with "Singh" anywhere in their name via
      `partial_ratio`, since "singh" is a perfect substring of
      "rajan bir singh thind". With strict ratio, that's ~24%.

    Returns the MAX across all metrics. The clinic-name path remains
    permissive (so genuine matches like "Saggar Dental Clinic" ↔
    "Saggar Dental Clinic" hit 1.0); the doctor-name path is the
    safety net that rejects token-substring false positives.
    """
    norm_lead = normalize_name(lead_name)
    if not norm_lead:
        return 0.0

    norm_clinic = normalize_name(candidate.clinic_name or "")
    norm_doctor = normalize_name(candidate.doctor_name or "")

    best_pct = 0.0

    if norm_clinic:
        for metric in (fuzz.partial_ratio, fuzz.token_set_ratio):
            v = metric(norm_lead, norm_clinic)
            if v > best_pct:
                best_pct = v
    if norm_doctor:
        for metric in (fuzz.ratio, fuzz.token_sort_ratio):
            v = metric(norm_lead, norm_doctor)
            if v > best_pct:
                best_pct = v

    return best_pct / 100.0


# ── controller ──────────────────────────────────────────────────────


class _PractoSearchProtocol(Protocol):
    """Structural type — what the controller needs from the gateway."""

    def search_dentists(
        self,
        *,
        query: str,
        city_slug: str,
        max_results: int = 10,
        now: datetime | None = None,
    ) -> PractoSearchOutcome: ...


@dataclass
class DiscoverPractoUrlsResult:
    """Aggregate stats for one `discover_for_leads` run.

    `n_already_known + n_attempted = total leads seen`. Within
    `n_attempted`, the buckets sum to: `n_matched + n_no_match +
    n_blocked + n_error`. (A `blocked` outcome is BOTH counted in
    n_blocked AND triggers `stopped_early=True`; remaining leads
    aren't even attempted.)
    """

    started_at: datetime
    finished_at: datetime | None = None
    n_attempted: int = 0
    n_matched: int = 0
    n_no_match: int = 0
    n_already_known: int = 0
    n_blocked: int = 0
    n_error: int = 0
    stopped_early: bool = False
    errors: list[str] = field(default_factory=list)


class DiscoverPractoUrlsController:
    """For each lead, search Practo and create a stub row when a
    candidate matches above the score threshold.

    See module docstring for the full match-and-persist policy.
    """

    def __init__(
        self,
        gateway: _PractoSearchProtocol,
        repo: PractoProfileRepository,
        *,
        max_candidates_per_search: int = 10,
        inter_search_seconds: float = 4.0,
        inter_search_jitter_seconds: float = 3.0,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        rng: Callable[[float, float], float] | None = None,
    ) -> None:
        if max_candidates_per_search < 1:
            raise ValueError("max_candidates_per_search must be >= 1")
        if inter_search_seconds < 0:
            raise ValueError("inter_search_seconds must be >= 0")
        if inter_search_jitter_seconds < 0:
            raise ValueError("inter_search_jitter_seconds must be >= 0")

        self._gateway = gateway
        self._repo = repo
        self._max_candidates = max_candidates_per_search
        self._inter_search_seconds = inter_search_seconds
        self._inter_search_jitter_seconds = inter_search_jitter_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._rng = rng or random.uniform
        self._sleep = sleep or _time.sleep

    # ── public API ──────────────────────────────────────────────────

    def discover_for_leads(
        self,
        leads: Iterable[RawLead],
        *,
        min_match_score: float = 0.7,
        dry_run: bool = False,
    ) -> DiscoverPractoUrlsResult:
        """Run discovery for each lead in `leads`.

        `min_match_score`: 0.0–1.0; the score threshold above which a
        candidate is treated as a match. Default 0.7.

        `dry_run`: when True, no upserts to the repo. Stats are
        computed and logged identically. Useful for tuning the
        threshold against a known-good lead set.

        Returns aggregate stats. Use `[lead]` for the per-lead form.
        """
        if not 0.0 <= min_match_score <= 1.0:
            raise ValueError("min_match_score must be in [0.0, 1.0]")

        leads_list = list(leads)
        result = DiscoverPractoUrlsResult(started_at=self._clock())

        logger.info(
            "practo.discover.start n={n} threshold={t} dry_run={d}",
            n=len(leads_list), t=min_match_score, d=dry_run,
        )

        for i, lead in enumerate(leads_list):
            stop, made_request = self._discover_one(
                lead, min_match_score, dry_run, result
            )
            if stop:
                result.stopped_early = True
                break
            # Polite pause only after an actual gateway call. Skipping
            # an already-known lead doesn't hit Practo, so no sleep.
            if made_request and i < len(leads_list) - 1:
                base = self._inter_search_seconds
                jitter = self._inter_search_jitter_seconds
                if base > 0 or jitter > 0:
                    pause = base + (
                        self._rng(0.0, jitter) if jitter > 0 else 0.0
                    )
                    self._sleep(pause)

        result.finished_at = self._clock()
        logger.info(
            "practo.discover.done attempted={a} matched={m} no_match={nm} "
            "already_known={ak} blocked={b} error={e} stopped_early={se}",
            a=result.n_attempted, m=result.n_matched, nm=result.n_no_match,
            ak=result.n_already_known, b=result.n_blocked, e=result.n_error,
            se=result.stopped_early,
        )
        return result

    # ── internals ───────────────────────────────────────────────────

    def _discover_one(
        self,
        lead: RawLead,
        min_match_score: float,
        dry_run: bool,
        result: DiscoverPractoUrlsResult,
    ) -> tuple[bool, bool]:
        """Process one lead.

        Returns `(stop_loop, made_search_request)`:
        - `stop_loop`  — True if the caller should stop the run
                         (Akamai block).
        - `made_search_request` — True if we hit the gateway. Used
                         to decide whether the caller should sleep
                         before the next iteration.
        """
        # Skip if any row exists. Idempotent on URL acquisition.
        if self._repo.get_by_place_id(lead.place_id) is not None:
            result.n_already_known += 1
            return False, False

        result.n_attempted += 1

        city_slug = practo_city_slug(lead.city)
        if not city_slug:
            msg = f"empty city slug for {lead.place_id} (city={lead.city!r})"
            result.n_error += 1
            result.errors.append(msg)
            logger.warning("practo.discover.no_city {msg}", msg=msg)
            return False, False

        outcome = self._gateway.search_dentists(
            query=lead.name,
            city_slug=city_slug,
            max_results=self._max_candidates,
            now=self._clock(),
        )

        if outcome.status == "blocked":
            result.n_blocked += 1
            if outcome.error_message:
                result.errors.append(outcome.error_message)
            logger.warning(
                "practo.discover.blocked place_id={pid} query={q!r}",
                pid=lead.place_id, q=lead.name,
            )
            return True, True

        if outcome.status == "error":
            result.n_error += 1
            if outcome.error_message:
                result.errors.append(outcome.error_message)
            logger.error(
                "practo.discover.error place_id={pid} query={q!r} err={e}",
                pid=lead.place_id, q=lead.name, e=outcome.error_message,
            )
            return False, True

        # status == "ok": score every candidate.
        best_score = 0.0
        best: PractoSearchResult | None = None
        for cand in outcome.candidates:
            s = score_candidate(lead.name, cand)
            if s > best_score:
                best_score = s
                best = cand

        if best is not None and best_score >= min_match_score:
            if not dry_run:
                self._repo.upsert_stub(lead.place_id, best.practo_url)
            result.n_matched += 1
            logger.info(
                "practo.discover.match place_id={pid} name={n!r} "
                "score={s:.2f} url={u}",
                pid=lead.place_id, n=lead.name, s=best_score,
                u=best.practo_url,
            )
        else:
            if not dry_run:
                self._upsert_no_url_found(lead, best_score)
            result.n_no_match += 1
            logger.info(
                "practo.discover.no_match place_id={pid} name={n!r} "
                "best_score={s:.2f} candidates={c}",
                pid=lead.place_id, n=lead.name, s=best_score,
                c=len(outcome.candidates),
            )
        return False, True

    def _upsert_no_url_found(self, lead: RawLead, best_score: float) -> None:
        """Persist a sentinel row so future passes skip this lead."""
        now = self._clock()
        profile = PractoProfile(
            place_id=lead.place_id,
            practo_url="",  # sentinel — see model docstring
            fetch_status="no_url_found",
            error_message=(
                f"best candidate score {best_score:.2f} below threshold"
            ),
            fetched_at=now,
            discovered_at=now,
            last_modified_at=now,
        )
        self._repo.upsert(profile, now=now)
