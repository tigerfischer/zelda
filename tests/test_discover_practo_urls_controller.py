"""Tests for the Practo URL-discovery controller.

Covers (a) the pure helpers — name normalization, fuzzy scoring, city
slug — in isolation, (b) the controller logic with a fake gateway.
The Playwright orchestration is smoke-tested live (see
scripts/smoke_practo_search.py).
"""

from datetime import datetime, timezone
from typing import Iterable

import pytest

from zelda.controllers.discover_practo_urls import (
    DiscoverPractoUrlsController,
    DiscoverPractoUrlsResult,
    normalize_name,
    practo_city_slug,
    score_candidate,
)
from zelda.gateways.practo_search import (
    PractoSearchOutcome,
    PractoSearchResult,
    PractoSearchStatus,
)
from zelda.models.raw_lead import RawLead
from zelda.repositories.practo_profile_repo import PractoProfileRepository


_T = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)


# ── city slug normalization ────────────────────────────────────────


def test_practo_city_slug_passes_simple_lowercase():
    assert practo_city_slug("Ludhiana") == "ludhiana"
    assert practo_city_slug("Mumbai") == "mumbai"
    assert practo_city_slug("Pune") == "pune"


def test_practo_city_slug_strips_whitespace():
    assert practo_city_slug("  Ludhiana  ") == "ludhiana"
    assert practo_city_slug("ludhi  ana") == "ludhi-ana"  # internal collapse


def test_practo_city_slug_handles_overrides():
    """Practo's slug differs from Google's city name for merged metros
    and renamed cities. Spot-check the ones we care about."""
    # Google sometimes returns "Bengaluru"; Practo uses "bangalore".
    assert practo_city_slug("Bengaluru") == "bangalore"
    # "New Delhi" (Google) → "delhi" (Practo).
    assert practo_city_slug("New Delhi") == "delhi"
    # Older spellings still floating around.
    assert practo_city_slug("Calcutta") == "kolkata"
    assert practo_city_slug("Bombay") == "mumbai"
    assert practo_city_slug("Madras") == "chennai"
    # Gurgaon was renamed to Gurugram but Practo kept the old slug.
    assert practo_city_slug("Gurugram") == "gurgaon"
    assert practo_city_slug("Gurgaon") == "gurgaon"


def test_practo_city_slug_hyphenates_unlisted_multi_word_cities():
    """Cities not in the override dict — e.g. unusual towns — fall
    through to lowercase + spaces→hyphens."""
    assert practo_city_slug("Bilaspur") == "bilaspur"
    assert practo_city_slug("Vasai Virar") == "vasai-virar"


def test_practo_city_slug_handles_empty_input():
    assert practo_city_slug("") == ""
    assert practo_city_slug("   ") == ""


# ── name normalization ─────────────────────────────────────────────


def test_normalize_name_lowercases_and_strips():
    assert normalize_name("Sai Dental Clinic") == "sai"
    assert normalize_name("  Sai  ") == "sai"


def test_normalize_name_strips_dr_salutation():
    assert normalize_name("Dr. K A Mohan") == "k a mohan"
    assert normalize_name("Doctor Kapoor") == "kapoor"
    assert normalize_name("Dr's Clinic") == ""  # all dropped: salut + clinic


def test_normalize_name_handles_dr_without_period():
    assert normalize_name("Dr Mohan") == "mohan"


def test_normalize_name_strips_clinic_suffix():
    assert normalize_name("Saggar Dental Clinic") == "saggar"
    # Iterative stripping peels stacked suffixes:
    # "smile care hospital" → "smile care" → "smile".
    assert normalize_name("Smile Care Hospital") == "smile"
    assert normalize_name("Apollo Centre") == "apollo"
    # Generic stacked-noise names → empty after iteration. Acceptable
    # because they're uninformative for fuzzy matching either way.
    assert normalize_name("Dental Care Multispeciality") == ""


def test_normalize_name_strips_seo_tail():
    """Google Places clinic names commonly include an SEO tail like
    'Sai Dental Clinic - Best Dentist Near Me in Ludhiana'. We must
    truncate at " - " or the city name in the tail will create
    spurious matches against unrelated candidates whose clinic name
    happens to be the city."""
    assert normalize_name(
        "Sai Dental Clinic - Best Dentist Near Me in Ludhiana"
    ) == "sai"
    # Other SEO separators we've seen.
    assert normalize_name("Sai Dental Clinic | Top Rated") == "sai"


def test_normalize_name_iteratively_strips_stacked_suffixes():
    """Real noisy Google name → discriminating prefix only."""
    assert normalize_name(
        "Saggar Dental Care Implant OPG & CBCT Centre"
    ) == "saggar"
    assert normalize_name(
        "Singh dental clinic and implant center"
    ) == "singh"


def test_normalize_name_strips_combined_salutation_and_suffix():
    """Real Google-style names: 'Dr. Kapoor's Dental Clinic' → 'kapoor s'."""
    out = normalize_name("Dr. Kapoor's Dental Clinic")
    assert out == "kapoor s"  # after stripping "dr.", "'s" → " s", "dental clinic"


def test_normalize_name_handles_unicode_accents():
    """NFKD decomposition strips combining marks."""
    assert normalize_name("Café Dental") == "cafe"
    assert normalize_name("Dr. José") == "jose"


def test_normalize_name_handles_unicode_math_bold():
    """Some clinic listings use unicode math-bold characters in their
    names. NFKD decomposes them to plain ASCII."""
    # 𝐒𝐚𝐢 is U+1D400 range, decomposes to "Sai".
    assert normalize_name("𝐒𝐚𝐢 Dental") == "sai"


def test_normalize_name_collapses_whitespace():
    assert normalize_name("Sai   Dental   Clinic") == "sai"


def test_normalize_name_handles_punctuation():
    assert normalize_name("Sai-Dental, Clinic.") == "sai"


def test_normalize_name_returns_empty_for_blank_input():
    assert normalize_name("") == ""
    assert normalize_name("   ") == ""
    assert normalize_name(None) == ""  # type: ignore[arg-type]


# ── score_candidate ────────────────────────────────────────────────


def _candidate(
    *,
    doctor_name: str | None = None,
    clinic_name: str | None = None,
) -> PractoSearchResult:
    return PractoSearchResult(
        practo_url="https://www.practo.com/x/doctor/y?practice_id=1",
        doctor_name=doctor_name,
        clinic_name=clinic_name,
        specialization=None,
        locality=None,
        profile_image_url=None,
        verified_badge=False,
        raw={},
    )


def test_score_candidate_high_when_clinic_name_matches():
    """The most common case: lead name (clinic) matches a candidate's
    clinic_name on Practo. Should score very high."""
    cand = _candidate(
        doctor_name="Dr. Ravi Saggar",
        clinic_name="Saggar Dental Clinic",
    )
    score = score_candidate("Saggar Dental Clinic", cand)
    assert score >= 0.95


def test_score_candidate_doctor_name_match_is_strict():
    """Doctor-name scoring is strict to avoid common-surname false
    positives. Lead 'Sai Dental Clinic' (→ 'sai') against doctor
    'Dr. Sai Kumar' (→ 'sai kumar') with a non-matching clinic name
    won't clear the threshold by itself — the candidate's own
    clinic_name has to also point at the same place."""
    cand = _candidate(
        doctor_name="Dr. Sai Kumar",
        clinic_name="Smile Studio",  # non-matching clinic name
    )
    score = score_candidate("Sai Dental Clinic", cand)
    assert score < 0.7


def test_score_candidate_doctor_name_exact_match_clears_threshold():
    """When the lead's discriminator IS the doctor's name (e.g. 'Saggar'
    where the clinic is named after Dr. Saggar), strict ratio still
    scores the doctor match high enough to match."""
    cand = _candidate(
        doctor_name="Dr. Saggar",
        clinic_name="Bright Smiles",  # non-matching clinic name
    )
    score = score_candidate("Saggar", cand)
    assert score >= 0.9


def test_score_candidate_rejects_common_surname_substring():
    """The 'Singh' false positive: lead 'Singh Dental Clinic' (→ 'singh')
    should NOT match a candidate whose doctor name contains 'singh' as
    a middle token (e.g. 'Dr. Rajan Bir Singh Thind' — a real Practo
    Ludhiana entry). Strict doctor-name scoring rejects this."""
    cand = _candidate(
        doctor_name="Dr. Rajan Bir Singh Thind",
        clinic_name="Thind Dental Clinic",  # also doesn't share "singh"
    )
    score = score_candidate("Singh Dental Clinic", cand)
    assert score < 0.7


def test_score_candidate_low_for_unrelated_names():
    cand = _candidate(
        doctor_name="Dr. Neeraj Goyal",
        clinic_name="Goyal Urology Centre",
    )
    score = score_candidate("Sai Dental Clinic", cand)
    assert score < 0.5


def test_score_candidate_handles_word_order_via_token_set():
    """token_set_ratio rescues this case: 'Smile Care Dental' vs
    'Care Smile Dentistry'. partial_ratio would be lower."""
    cand = _candidate(clinic_name="Care Smile Dentistry")
    score = score_candidate("Smile Care", cand)
    assert score >= 0.85


def test_score_candidate_zero_for_empty_lead_or_candidate():
    assert score_candidate("", _candidate(doctor_name="Dr. X")) == 0.0
    assert score_candidate("Foo", _candidate()) == 0.0


def test_score_candidate_uses_max_of_doctor_and_clinic_field():
    """The clinic name doesn't match but the doctor name does —
    max() should pick up the doctor signal."""
    cand = _candidate(
        doctor_name="Dr. Saggar",
        clinic_name="Bright Smiles",
    )
    score = score_candidate("Saggar Dental", cand)
    assert score >= 0.9


# ── controller: fake gateway ───────────────────────────────────────


class FakePractoSearchGateway:
    """Drives controller tests without launching Playwright.

    Configured per test via `responses` — a mapping of
    `(query, city_slug)` to a `PractoSearchOutcome`. Records every
    call for assertion.
    """

    def __init__(
        self,
        responses: dict[tuple[str, str], PractoSearchOutcome] | None = None,
    ) -> None:
        self.responses: dict[tuple[str, str], PractoSearchOutcome] = (
            dict(responses) if responses else {}
        )
        self.calls: list[dict] = []

    def search_dentists(
        self,
        *,
        query: str,
        city_slug: str,
        max_results: int = 10,
        now: datetime | None = None,
    ) -> PractoSearchOutcome:
        self.calls.append(
            {
                "query": query,
                "city_slug": city_slug,
                "max_results": max_results,
                "now": now,
            }
        )
        key = (query, city_slug)
        if key not in self.responses:
            raise AssertionError(
                f"no scripted response for search_dentists({key})"
            )
        return self.responses[key]


def _outcome(
    *,
    query: str = "Sai Dental Clinic",
    city_slug: str = "ludhiana",
    status: PractoSearchStatus = "ok",
    candidates: Iterable[PractoSearchResult] = (),
    error_message: str | None = None,
) -> PractoSearchOutcome:
    return PractoSearchOutcome(
        query=query,
        city_slug=city_slug,
        searched_at=_T,
        status=status,
        candidates=list(candidates),
        error_message=error_message,
    )


def _lead(
    place_id: str = "ChIJ_X",
    name: str = "Sai Dental Clinic",
    city: str = "Ludhiana",
) -> RawLead:
    return RawLead(
        place_id=place_id, city=city, name=name,
        discovered_at=_T, last_modified_at=_T,
    )


@pytest.fixture
def repo():
    r = PractoProfileRepository(":memory:")
    yield r
    r.close()


# ── empty input + already-known short-circuit ─────────────────────


def test_discover_for_leads_empty_list_is_noop(repo):
    gw = FakePractoSearchGateway()
    ctrl = DiscoverPractoUrlsController(
        gw, repo,
        inter_search_seconds=0, inter_search_jitter_seconds=0,
        clock=lambda: _T, sleep=lambda _: None,
    )
    result = ctrl.discover_for_leads([])
    assert result.n_attempted == 0
    assert result.n_matched == 0
    assert gw.calls == []


def test_discover_skips_leads_with_existing_profile_row(repo):
    """Any existing row (pending / ok / no_url_found / error / blocked)
    means we don't re-discover."""
    repo.upsert_stub("ChIJ_KNOWN", "https://www.practo.com/x/doctor/y", now=_T)

    gw = FakePractoSearchGateway()  # no responses scripted; would error if called
    ctrl = DiscoverPractoUrlsController(
        gw, repo,
        inter_search_seconds=0, inter_search_jitter_seconds=0,
        clock=lambda: _T, sleep=lambda _: None,
    )
    result = ctrl.discover_for_leads([_lead("ChIJ_KNOWN")])
    assert result.n_already_known == 1
    assert result.n_attempted == 0
    assert gw.calls == []


# ── high-confidence match ─────────────────────────────────────────


def test_discover_creates_stub_for_high_match(repo):
    cand = PractoSearchResult(
        practo_url="https://www.practo.com/ludhiana/doctor/dr-saggar?practice_id=42",
        doctor_name="Dr. Ravi Saggar",
        clinic_name="Saggar Dental Clinic",
        specialization="Dentist",
        locality="Model Town",
        profile_image_url=None,
        verified_badge=True,
        raw={},
    )
    gw = FakePractoSearchGateway(responses={
        ("Saggar Dental Clinic", "ludhiana"): _outcome(
            query="Saggar Dental Clinic", candidates=[cand]
        ),
    })
    ctrl = DiscoverPractoUrlsController(
        gw, repo,
        inter_search_seconds=0, inter_search_jitter_seconds=0,
        clock=lambda: _T, sleep=lambda _: None,
    )
    result = ctrl.discover_for_leads(
        [_lead("ChIJ_A", name="Saggar Dental Clinic", city="Ludhiana")]
    )
    assert result.n_matched == 1
    assert result.n_no_match == 0
    persisted = repo.get_by_place_id("ChIJ_A")
    assert persisted is not None
    assert persisted.fetch_status == "pending"
    assert persisted.practo_url == cand.practo_url


def test_discover_picks_highest_scoring_candidate(repo):
    """Two candidates returned. Best score wins (above threshold)."""
    decoy = PractoSearchResult(
        practo_url="https://www.practo.com/ludhiana/doctor/dr-other?practice_id=1",
        doctor_name="Dr. Other",
        clinic_name="Unrelated Clinic",
        specialization="Cardiologist",
        locality="X",
        profile_image_url=None,
        verified_badge=False,
        raw={},
    )
    real_match = PractoSearchResult(
        practo_url="https://www.practo.com/ludhiana/doctor/dr-saggar?practice_id=42",
        doctor_name="Dr. Saggar",
        clinic_name="Saggar Dental Clinic",
        specialization="Dentist",
        locality="X",
        profile_image_url=None,
        verified_badge=False,
        raw={},
    )
    gw = FakePractoSearchGateway(responses={
        ("Saggar Dental Clinic", "ludhiana"): _outcome(
            query="Saggar Dental Clinic", candidates=[decoy, real_match],
        ),
    })
    ctrl = DiscoverPractoUrlsController(
        gw, repo,
        inter_search_seconds=0, inter_search_jitter_seconds=0,
        clock=lambda: _T, sleep=lambda _: None,
    )
    ctrl.discover_for_leads(
        [_lead("ChIJ_A", name="Saggar Dental Clinic")]
    )
    persisted = repo.get_by_place_id("ChIJ_A")
    assert persisted.practo_url == real_match.practo_url


# ── low-confidence: no match ──────────────────────────────────────


def test_discover_persists_no_url_found_when_below_threshold(repo):
    weak = PractoSearchResult(
        practo_url="https://www.practo.com/ludhiana/doctor/dr-x?practice_id=1",
        doctor_name="Dr. Completely Different",
        clinic_name="Totally Other Clinic",
        specialization="Cardiologist",
        locality="X",
        profile_image_url=None,
        verified_badge=False,
        raw={},
    )
    gw = FakePractoSearchGateway(responses={
        ("Sai Dental Clinic", "ludhiana"): _outcome(
            query="Sai Dental Clinic", candidates=[weak],
        ),
    })
    ctrl = DiscoverPractoUrlsController(
        gw, repo,
        inter_search_seconds=0, inter_search_jitter_seconds=0,
        clock=lambda: _T, sleep=lambda _: None,
    )
    result = ctrl.discover_for_leads([_lead("ChIJ_LOW")])
    assert result.n_matched == 0
    assert result.n_no_match == 1
    persisted = repo.get_by_place_id("ChIJ_LOW")
    assert persisted is not None
    assert persisted.fetch_status == "no_url_found"
    assert persisted.practo_url == ""
    assert persisted.error_message and "below threshold" in persisted.error_message


def test_discover_no_match_when_no_candidates(repo):
    gw = FakePractoSearchGateway(responses={
        ("Sai Dental Clinic", "ludhiana"): _outcome(
            query="Sai Dental Clinic", candidates=[],
        ),
    })
    ctrl = DiscoverPractoUrlsController(
        gw, repo,
        inter_search_seconds=0, inter_search_jitter_seconds=0,
        clock=lambda: _T, sleep=lambda _: None,
    )
    result = ctrl.discover_for_leads([_lead("ChIJ_NONE")])
    assert result.n_no_match == 1
    persisted = repo.get_by_place_id("ChIJ_NONE")
    assert persisted.fetch_status == "no_url_found"


# ── blocked / error semantics ─────────────────────────────────────


def test_discover_stops_loop_when_blocked(repo):
    cand = PractoSearchResult(
        practo_url="https://www.practo.com/x/doctor/dr-a?practice_id=1",
        doctor_name="Dr. A", clinic_name="A Clinic",
        specialization=None, locality=None,
        profile_image_url=None, verified_badge=False, raw={},
    )
    gw = FakePractoSearchGateway(responses={
        ("First Clinic", "ludhiana"): _outcome(
            query="First Clinic", candidates=[cand],
        ),
        ("Blocked Clinic", "ludhiana"): _outcome(
            query="Blocked Clinic", status="blocked",
            error_message="akamai challenge page",
        ),
        # No third response scripted — if controller didn't stop the
        # loop, the fake would raise.
    })
    ctrl = DiscoverPractoUrlsController(
        gw, repo,
        inter_search_seconds=0, inter_search_jitter_seconds=0,
        clock=lambda: _T, sleep=lambda _: None,
    )
    leads = [
        _lead("ChIJ_A", name="First Clinic"),
        _lead("ChIJ_B", name="Blocked Clinic"),
        _lead("ChIJ_C", name="Never Reached"),
    ]
    # Need to ensure ChIJ_A actually matches; rename clinic to one that
    # scores high.
    cand2 = PractoSearchResult(
        practo_url=cand.practo_url, doctor_name="Dr. First",
        clinic_name="First Clinic", specialization=None, locality=None,
        profile_image_url=None, verified_badge=False, raw={},
    )
    gw.responses[("First Clinic", "ludhiana")] = _outcome(
        query="First Clinic", candidates=[cand2],
    )
    result = ctrl.discover_for_leads(leads)
    assert result.stopped_early is True
    assert result.n_blocked == 1
    assert result.n_attempted == 2  # A and B tried; C never reached
    # B persists no_url_found? NO — blocked means we never got a chance
    # to assess; we don't persist anything for blocked leads.
    assert repo.get_by_place_id("ChIJ_B") is None
    # C is untouched and remains discoverable on the next run.
    assert repo.get_by_place_id("ChIJ_C") is None


def test_discover_continues_past_errors(repo):
    cand = PractoSearchResult(
        practo_url="https://www.practo.com/x/doctor/y?practice_id=1",
        doctor_name="Dr. B", clinic_name="B Clinic",
        specialization=None, locality=None,
        profile_image_url=None, verified_badge=False, raw={},
    )
    gw = FakePractoSearchGateway(responses={
        ("A Clinic", "ludhiana"): _outcome(
            query="A Clinic", status="error",
            error_message="playwright timeout",
        ),
        ("B Clinic", "ludhiana"): _outcome(
            query="B Clinic", candidates=[cand],
        ),
    })
    ctrl = DiscoverPractoUrlsController(
        gw, repo,
        inter_search_seconds=0, inter_search_jitter_seconds=0,
        clock=lambda: _T, sleep=lambda _: None,
    )
    result = ctrl.discover_for_leads([
        _lead("ChIJ_A", name="A Clinic"),
        _lead("ChIJ_B", name="B Clinic"),
    ])
    assert not result.stopped_early
    assert result.n_error == 1
    assert result.n_matched == 1
    # Errored row should NOT have a persisted state — next run retries.
    assert repo.get_by_place_id("ChIJ_A") is None
    assert repo.get_by_place_id("ChIJ_B") is not None


# ── dry_run mode ──────────────────────────────────────────────────


def test_discover_dry_run_computes_stats_without_persisting(repo):
    cand = PractoSearchResult(
        practo_url="https://www.practo.com/x/doctor/y?practice_id=1",
        doctor_name="Dr. Saggar", clinic_name="Saggar Dental Clinic",
        specialization="Dentist", locality="X",
        profile_image_url=None, verified_badge=False, raw={},
    )
    gw = FakePractoSearchGateway(responses={
        ("Saggar Dental Clinic", "ludhiana"): _outcome(
            query="Saggar Dental Clinic", candidates=[cand],
        ),
        ("Unmatched Clinic", "ludhiana"): _outcome(
            query="Unmatched Clinic", candidates=[],
        ),
    })
    ctrl = DiscoverPractoUrlsController(
        gw, repo,
        inter_search_seconds=0, inter_search_jitter_seconds=0,
        clock=lambda: _T, sleep=lambda _: None,
    )
    result = ctrl.discover_for_leads(
        [
            _lead("ChIJ_A", name="Saggar Dental Clinic"),
            _lead("ChIJ_B", name="Unmatched Clinic"),
        ],
        dry_run=True,
    )
    assert result.n_matched == 1
    assert result.n_no_match == 1
    # Nothing persisted in dry-run.
    assert repo.get_by_place_id("ChIJ_A") is None
    assert repo.get_by_place_id("ChIJ_B") is None


# ── city slug failure path ─────────────────────────────────────────


def test_discover_records_error_when_city_slug_empty(repo):
    """A lead with no usable city is an error, not a search miss."""
    gw = FakePractoSearchGateway()
    ctrl = DiscoverPractoUrlsController(
        gw, repo,
        inter_search_seconds=0, inter_search_jitter_seconds=0,
        clock=lambda: _T, sleep=lambda _: None,
    )
    result = ctrl.discover_for_leads([_lead("ChIJ_NOCITY", city="")])
    assert result.n_error == 1
    assert gw.calls == []
    assert any("city" in e.lower() for e in result.errors)


# ── inter_search rate limiting ────────────────────────────────────


def test_inter_search_sleep_only_after_real_searches(repo):
    """A skipped (already-known) lead should not trigger a sleep —
    we didn't hit Practo, so politeness isn't owed."""
    # Pre-seed: ChIJ_KNOWN already has a row.
    repo.upsert_stub("ChIJ_KNOWN", "https://www.practo.com/x/doctor/y", now=_T)

    cand = PractoSearchResult(
        practo_url="https://www.practo.com/x/doctor/z?practice_id=2",
        doctor_name="Dr. New", clinic_name="New Clinic",
        specialization=None, locality=None,
        profile_image_url=None, verified_badge=False, raw={},
    )
    gw = FakePractoSearchGateway(responses={
        ("New Clinic", "ludhiana"): _outcome(
            query="New Clinic", candidates=[cand],
        ),
    })
    sleeps: list[float] = []
    ctrl = DiscoverPractoUrlsController(
        gw, repo,
        inter_search_seconds=4.0, inter_search_jitter_seconds=0,
        clock=lambda: _T, sleep=sleeps.append,
    )
    # Order: known, search → skipped, then searched. No sleep needed
    # (only one actual gateway call, no subsequent iteration).
    result = ctrl.discover_for_leads([
        _lead("ChIJ_KNOWN"),
        _lead("ChIJ_NEW", name="New Clinic"),
    ])
    assert result.n_already_known == 1
    assert result.n_matched == 1
    assert sleeps == []  # no sleep after last iteration


def test_inter_search_sleep_includes_jitter(repo):
    cand_a = PractoSearchResult(
        practo_url="https://www.practo.com/x/doctor/a?practice_id=1",
        doctor_name="Dr. A", clinic_name="A Clinic",
        specialization=None, locality=None,
        profile_image_url=None, verified_badge=False, raw={},
    )
    cand_b = PractoSearchResult(
        practo_url="https://www.practo.com/x/doctor/b?practice_id=2",
        doctor_name="Dr. B", clinic_name="B Clinic",
        specialization=None, locality=None,
        profile_image_url=None, verified_badge=False, raw={},
    )
    gw = FakePractoSearchGateway(responses={
        ("A Clinic", "ludhiana"): _outcome(
            query="A Clinic", candidates=[cand_a],
        ),
        ("B Clinic", "ludhiana"): _outcome(
            query="B Clinic", candidates=[cand_b],
        ),
    })
    sleeps: list[float] = []
    rng_calls: list[tuple[float, float]] = []

    def fake_rng(lo: float, hi: float) -> float:
        rng_calls.append((lo, hi))
        return 1.5

    ctrl = DiscoverPractoUrlsController(
        gw, repo,
        inter_search_seconds=4.0, inter_search_jitter_seconds=3.0,
        clock=lambda: _T, sleep=sleeps.append, rng=fake_rng,
    )
    ctrl.discover_for_leads([
        _lead("ChIJ_A", name="A Clinic"),
        _lead("ChIJ_B", name="B Clinic"),
    ])
    # One sleep between the two leads: 4.0 + 1.5 = 5.5.
    assert sleeps == [5.5]
    assert rng_calls == [(0.0, 3.0)]


# ── input validation ──────────────────────────────────────────────


def test_init_rejects_bad_args(repo):
    gw = FakePractoSearchGateway()
    with pytest.raises(ValueError):
        DiscoverPractoUrlsController(
            gw, repo, max_candidates_per_search=0,
        )
    with pytest.raises(ValueError):
        DiscoverPractoUrlsController(
            gw, repo, inter_search_seconds=-1.0,
        )
    with pytest.raises(ValueError):
        DiscoverPractoUrlsController(
            gw, repo, inter_search_jitter_seconds=-0.1,
        )


def test_discover_rejects_invalid_threshold(repo):
    gw = FakePractoSearchGateway()
    ctrl = DiscoverPractoUrlsController(gw, repo)
    with pytest.raises(ValueError):
        ctrl.discover_for_leads([_lead()], min_match_score=1.5)
    with pytest.raises(ValueError):
        ctrl.discover_for_leads([_lead()], min_match_score=-0.1)


# ── single-lead form (orchestrator API shape) ─────────────────────


def test_discover_for_single_lead_works(repo):
    """The orchestrator uses `discover_for_leads([lead])` as a per-lead
    prerequisite step. Verify that path works cleanly."""
    cand = PractoSearchResult(
        practo_url="https://www.practo.com/x/doctor/y?practice_id=1",
        doctor_name="Dr. Saggar", clinic_name="Saggar Dental Clinic",
        specialization="Dentist", locality="X",
        profile_image_url=None, verified_badge=True, raw={},
    )
    gw = FakePractoSearchGateway(responses={
        ("Saggar Dental Clinic", "ludhiana"): _outcome(
            query="Saggar Dental Clinic", candidates=[cand],
        ),
    })
    ctrl = DiscoverPractoUrlsController(
        gw, repo,
        inter_search_seconds=0, inter_search_jitter_seconds=0,
        clock=lambda: _T, sleep=lambda _: None,
    )
    one_lead = _lead("ChIJ_X", name="Saggar Dental Clinic")
    result = ctrl.discover_for_leads([one_lead])
    assert isinstance(result, DiscoverPractoUrlsResult)
    assert result.n_matched == 1
    assert repo.get_by_place_id("ChIJ_X").practo_url == cand.practo_url
