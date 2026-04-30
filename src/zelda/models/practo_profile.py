"""PractoProfile — one Practo doctor/clinic profile, scraped via
`PractoPlaywrightGateway` and joined to a Google Places lead by
`place_id`.

Lifecycle
---------
A row passes through these states:

1. **stub** (`fetch_status="pending"`, `fetched_at=None`). A Practo
   URL has been associated with a place_id (manually, or by the
   URL-discovery controller) but enrichment hasn't run yet.
2. **enriched** (`fetch_status="ok"`, `fetched_at=<utc>`). The
   gateway parsed the profile cleanly; well-known signals are
   promoted to typed columns and the full Redux state is preserved
   in `raw_json`.
3. **terminal-failure** (`fetch_status="not_found" | "no_url_found"
   | "blocked" | "error"`). Either no usable profile exists or the
   gateway hit a transient block. `error_message` carries the
   reason; callers decide whether to retry.

Why a separate table (rather than fields on `RawLead`)
-----------------------------------------------------
Practo data is rich (~30+ fields), refreshes on a different cadence
(monthly vs. weekly for Places), and follows a one-to-one mapping
with `place_id`. Bolting it onto `RawLead` would bloat that model
and entangle two refresh contracts. Future enrichment sources
(JustDial, Lybrate, IDA) will follow the same per-source-table pattern.

Field selection
---------------
The typed columns prioritize signals from `docs/enrichment-signals.md`
(A9, A10, D8, D9, E4, F1, F3, G1, G5, G10, G11, G12) plus high-value
fields the gateway reliably returns (clinic address, services,
photo URLs, summary, recommendation %). Anything else is preserved
in `raw_json` so we never lose information even if Practo changes
its Redux shape.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


PractoFetchStatus = Literal[
    "pending",        # URL known, gateway not yet run
    "ok",             # profile parsed successfully
    "not_found",      # 404 / no Redux state — the URL is dead or wrong
    "no_url_found",   # discovery searched Practo but no candidate matched
    "blocked",        # Akamai challenge intercepted us; pause and retry later
    "error",          # unexpected exception during navigation / parsing
]
"""How the most recent fetch attempt for this row went.

`pending` is the only status with `fetched_at=None`. The five
terminal statuses (`ok`, `not_found`, `no_url_found`, `blocked`,
`error`) all have `fetched_at` populated so callers can decide
retry policy based on age.

`no_url_found` is set by the URL-discovery controller (which
searches Practo by name) when no candidate clears the match
threshold. Persisting it lets the orchestrator skip future
discovery for the same lead until it's manually re-tried — without
this, every orchestrator pass would re-search the same dead leads.
The row carries no `practo_url` (or a placeholder); it's a
sentinel telling the enrichment controller "don't bother, there's
nothing to fetch".

`blocked` is special — it signals an environmental problem (Akamai
fingerprinted us) rather than a per-URL one. The controller stops
the loop on `blocked` so we don't burn through the queue against a
flagged session.
"""


class PractoProfile(BaseModel):
    """A scraped Practo profile bound to a `place_id`.

    Typed columns hold the signals we care about explicitly. `raw_json`
    holds the full Apify response so we never lose fields the actor
    starts emitting (or renames) over time.
    """

    model_config = ConfigDict(extra="ignore")

    # ── identity / FK ────────────────────────────────────────────────

    place_id: str
    """Foreign key to `raw_leads.place_id`. One Practo profile per lead."""

    practo_url: str
    """The Practo profile URL associated with this lead. Either set
    manually by an operator or by the URL-discovery controller when a
    candidate cleared the match threshold. The enrichment gateway
    navigates to this URL.

    Empty string `""` is permitted only when `fetch_status='no_url_found'`
    — a sentinel row left by the discovery controller to record that
    we searched and found no match, so future discovery passes can
    skip this lead. The enrichment controller filters by status and
    won't try to fetch from an empty URL.
    """

    # ── core identity (G1) ───────────────────────────────────────────

    practo_doctor_id: str | None = None
    """Practo's internal doctor ID, if surfaced by the actor."""

    profile_url: str | None = None
    """Canonical Practo profile URL the actor returns. May differ from
    `practo_url` if the input was a search URL."""

    name: str | None = None
    """Doctor's display name as Practo shows it ('Dr. K A Mohan')."""

    # ── credentials (G5, G10, G11, G12) ──────────────────────────────

    qualifications: list[str] = Field(default_factory=list)
    """Degree codes — e.g. ['BDS', 'MDS - Orthodontics']."""

    experience_years: int | None = None
    """Total years of clinical experience (Practo's `years_of_experience`)."""

    specializations: list[str] = Field(default_factory=list)
    """Subspecialty names — e.g. ['Orthodontist', 'Dental Surgeon']."""

    languages: list[str] = Field(default_factory=list)
    """Languages the doctor consults in."""

    registrations: list[dict[str, Any]] = Field(default_factory=list)
    """Council registrations (council name, registration number, year)."""

    education: list[dict[str, Any]] = Field(default_factory=list)
    """Education history (college, degree, year)."""

    awards: list[dict[str, Any]] = Field(default_factory=list)
    """Awards / honors with year and citation."""

    memberships: list[str] = Field(default_factory=list)
    """Professional memberships (IDA, FDI, etc.)."""

    # ── practice / clinic (F1) ──────────────────────────────────────

    clinic_name: str | None = None
    clinic_address: str | None = None
    clinic_locality: str | None = None
    clinic_city: str | None = None

    consultation_fee: int | None = None
    """Consultation fee in `consultation_fee_currency` units. Practo
    typically returns INR for India."""
    consultation_fee_currency: str | None = None

    services: list[str] = Field(default_factory=list)
    """Services / procedures the practitioner offers."""

    operating_hours: dict[str, Any] | list[dict[str, Any]] | None = None
    """Day-of-week opening hours. The Apify actor currently returns a
    list of `{begin_time, end_time, available_days}` slot dicts, but
    we accept dict-shaped values too in case the actor's schema
    changes (or another source is wired in)."""

    # ── geographic ──────────────────────────────────────────────────

    lat: float | None = None
    lng: float | None = None

    # ── reputation (D8, D9) ─────────────────────────────────────────

    recommendation_percent: int | None = None
    """Practo's 'patient recommendation' percentage (D9). The fraction
    of feedback respondents who said they would recommend the doctor.
    Range 0–100."""

    rating: float | None = None
    """Numeric rating where Practo exposes one. Practo's *doctor*
    profiles use recommendation_percent as the headline metric;
    `rating` is more typically populated for clinics."""

    reviews_count: int | None = None
    """Total Practo feedback responses (how many patients gave
    feedback). On the page this is "X feedbacks"."""

    patient_count: int | None = None
    """Total patients seen — Practo sometimes exposes this separately
    from the feedback count."""

    # ── agency-engagement signals ──────────────────────────────────

    has_practo_plus_badge: bool | None = None
    """E4 — whether the doctor / clinic shows the Practo Plus / Prime
    badge on this profile (i.e. they're paying Practo's subscription
    program). Inferred from the `is_prime_doctor` flag in Practo's
    state. None when the source field is absent (older HTML, partial
    fetch); use `is None` to distinguish "unknown" from `False`."""

    # ── operations / availability (B5) ─────────────────────────────

    next_available_at: datetime | None = None
    """B5 — earliest available appointment slot Practo's booking
    system shows for this doctor. None if Practo isn't running
    booking for them, the schedule is empty, or the field is missing
    from this fetch."""

    # ── media (A10) ─────────────────────────────────────────────────

    profile_image_url: str | None = None
    photo_urls: list[str] = Field(default_factory=list)

    # ── bio ─────────────────────────────────────────────────────────

    summary: str | None = None
    """Free-text bio / description."""

    # ── capture metadata ────────────────────────────────────────────

    fetch_status: PractoFetchStatus = "pending"
    error_message: str | None = None

    fetched_at: datetime | None = None
    """When the most recent successful or failed fetch happened. None
    only when `fetch_status='pending'`."""

    raw_json: dict[str, Any] = Field(default_factory=dict)
    """Full Apify record as returned. Source of truth for any field
    we haven't promoted to a typed column."""

    discovered_at: datetime
    """When the URL was first associated with this place_id (i.e.
    when the stub row was first created)."""

    last_modified_at: datetime
    """Bumped on every upsert — used for downstream sync / refresh
    cadence decisions."""

    # ── derived properties ──────────────────────────────────────────

    @property
    def is_pending(self) -> bool:
        return self.fetch_status == "pending"

    @property
    def is_enriched(self) -> bool:
        """True iff this row carries usable scraped data."""
        return self.fetch_status == "ok"
