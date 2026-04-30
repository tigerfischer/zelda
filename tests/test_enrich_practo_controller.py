from datetime import datetime, timezone
from typing import Callable

import pytest

from zelda.controllers.enrich_practo import EnrichPractoController
from zelda.gateways.practo_playwright import PractoFetchResult
from zelda.models.practo_profile import PractoFetchStatus, PractoProfile
from zelda.repositories.practo_profile_repo import PractoProfileRepository


_T = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)


# ── fake gateway ────────────────────────────────────────────────────


class FakePractoGateway:
    """Drives controller tests without launching Playwright.

    Configured per test via `responses` — a list of (status, profile,
    error_message) tuples consumed in call order. The fake mirrors the
    real gateway's contract: the profile field is ALWAYS non-None
    (terminal-failure profiles are stub-shaped).
    """

    def __init__(
        self,
        responses: (
            list[tuple[PractoFetchStatus, PractoProfile | None, str | None]] | None
        ) = None,
    ) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def fetch_profile(
        self,
        *,
        place_id: str,
        practo_url: str,
        now: datetime | None = None,
    ) -> PractoFetchResult:
        self.calls.append(
            {"place_id": place_id, "practo_url": practo_url, "now": now}
        )
        if not self.responses:
            raise AssertionError(
                f"no scripted response for fetch_profile({place_id})"
            )
        status, profile, err = self.responses.pop(0)
        # Synthesize a stub profile if the test didn't supply one — the
        # real gateway always returns a profile, never None.
        if profile is None:
            profile = PractoProfile(
                place_id=place_id,
                practo_url=practo_url,
                fetch_status=status,
                fetched_at=now or _T,
                error_message=err,
                discovered_at=now or _T,
                last_modified_at=now or _T,
            )
        return PractoFetchResult(
            practo_url=practo_url,
            fetched_at=now or _T,
            status=status,
            profile=profile,
            error_message=err,
        )


# ── builders ────────────────────────────────────────────────────────


def _ok_profile(place_id: str, practo_url: str, name: str = "Dr. X") -> PractoProfile:
    return PractoProfile(
        place_id=place_id,
        practo_url=practo_url,
        name=name,
        consultation_fee=500,
        reviews_count=42,
        recommendation_percent=73,
        fetch_status="ok",
        fetched_at=_T,
        discovered_at=_T,
        last_modified_at=_T,
    )


def _not_found_profile(place_id: str, practo_url: str) -> PractoProfile:
    return PractoProfile(
        place_id=place_id,
        practo_url=practo_url,
        fetch_status="not_found",
        fetched_at=_T,
        discovered_at=_T,
        last_modified_at=_T,
    )


def _blocked_profile(place_id: str, practo_url: str) -> PractoProfile:
    return PractoProfile(
        place_id=place_id,
        practo_url=practo_url,
        fetch_status="blocked",
        fetched_at=_T,
        error_message="akamai challenge page",
        discovered_at=_T,
        last_modified_at=_T,
    )


@pytest.fixture
def repo():
    r = PractoProfileRepository(":memory:")
    yield r
    r.close()


def _make_clock(times: list[datetime]) -> Callable[[], datetime]:
    """Returns a clock that yields each time in turn, repeating the last."""
    iterator = iter(times)
    last = times[-1]

    def clock() -> datetime:
        nonlocal last
        try:
            last = next(iterator)
        except StopIteration:
            pass
        return last

    return clock


# ── enrich_pending happy path ───────────────────────────────────────


def test_enrich_pending_processes_all_stubs(repo):
    repo.upsert_stub("ChIJ_A", "https://a", now=_T)
    repo.upsert_stub("ChIJ_B", "https://b", now=_T)

    gw = FakePractoGateway(responses=[
        ("ok", _ok_profile("ChIJ_A", "https://a", "Dr. A"), None),
        ("ok", _ok_profile("ChIJ_B", "https://b", "Dr. B"), None),
    ])
    ctrl = EnrichPractoController(
        gw, repo, inter_lead_seconds=0, clock=lambda: _T, sleep=lambda _: None
    )

    result = ctrl.enrich_pending()

    assert result.n_attempted == 2
    assert result.n_ok == 2
    assert result.n_not_found == 0
    assert not result.stopped_early

    a = repo.get_by_place_id("ChIJ_A")
    assert a is not None
    assert a.fetch_status == "ok"
    assert a.name == "Dr. A"

    b = repo.get_by_place_id("ChIJ_B")
    assert b.name == "Dr. B"


def test_enrich_pending_skips_already_enriched_rows(repo):
    """Already-enriched rows are not picked up by `get_pending` and so
    aren't re-fetched. Important for cost control."""
    repo.upsert_stub("ChIJ_A", "https://a", now=_T)
    repo.upsert(_ok_profile("ChIJ_B", "https://b", "Dr. B"))

    gw = FakePractoGateway(responses=[
        ("ok", _ok_profile("ChIJ_A", "https://a", "Dr. A"), None),
    ])
    ctrl = EnrichPractoController(
        gw, repo, inter_lead_seconds=0, clock=lambda: _T, sleep=lambda _: None
    )

    result = ctrl.enrich_pending()

    assert result.n_attempted == 1
    assert {c["place_id"] for c in gw.calls} == {"ChIJ_A"}


def test_enrich_pending_respects_max_leads(repo):
    for i in range(5):
        repo.upsert_stub(f"ChIJ_{i}", f"https://{i}", now=_T)

    responses = [("ok", _ok_profile(f"ChIJ_{i}", f"https://{i}"), None) for i in range(5)]
    gw = FakePractoGateway(responses=responses)
    ctrl = EnrichPractoController(
        gw, repo, inter_lead_seconds=0, clock=lambda: _T, sleep=lambda _: None
    )

    result = ctrl.enrich_pending(max_leads=2)

    assert result.n_attempted == 2
    assert len(gw.calls) == 2


def test_enrich_pending_preserves_discovered_at_from_stub(repo):
    repo.upsert_stub("ChIJ_A", "https://a", now=_T)
    later = _T.replace(year=_T.year + 1)
    gw = FakePractoGateway(responses=[
        ("ok", _ok_profile("ChIJ_A", "https://a"), None),
    ])
    ctrl = EnrichPractoController(
        gw, repo, inter_lead_seconds=0, clock=lambda: later, sleep=lambda _: None
    )

    ctrl.enrich_pending()

    a = repo.get_by_place_id("ChIJ_A")
    assert a.discovered_at == _T  # stub time preserved
    assert a.last_modified_at == later  # bumped on enrich


# ── not_found ─────────────────────────────────────────────────────


def test_enrich_pending_persists_not_found_as_terminal(repo):
    """Dead URL → status='not_found' so we don't re-fetch on next run."""
    repo.upsert_stub("ChIJ_A", "https://dead", now=_T)
    gw = FakePractoGateway(responses=[
        ("not_found", _not_found_profile("ChIJ_A", "https://dead"), None),
    ])
    ctrl = EnrichPractoController(
        gw, repo, inter_lead_seconds=0, clock=lambda: _T, sleep=lambda _: None
    )

    result = ctrl.enrich_pending()

    assert result.n_not_found == 1
    a = repo.get_by_place_id("ChIJ_A")
    assert a.fetch_status == "not_found"
    # Re-run the controller — the not_found row should NOT be picked up.
    gw2 = FakePractoGateway(responses=[])
    ctrl2 = EnrichPractoController(
        gw2, repo, inter_lead_seconds=0, clock=lambda: _T, sleep=lambda _: None
    )
    result2 = ctrl2.enrich_pending()
    assert result2.n_attempted == 0


# ── blocked halts the run ─────────────────────────────────────────


def test_enrich_pending_stops_when_blocked(repo):
    """Akamai challenge → halt the run. Once flagged, every subsequent
    request from the same session will hit the same wall, so we stop
    and let the next run (with a fresh context) resume."""
    repo.upsert_stub("ChIJ_A", "https://a", now=_T)
    repo.upsert_stub("ChIJ_B", "https://b", now=_T.replace(microsecond=1))
    repo.upsert_stub("ChIJ_C", "https://c", now=_T.replace(microsecond=2))

    gw = FakePractoGateway(responses=[
        ("ok", _ok_profile("ChIJ_A", "https://a"), None),
        ("blocked", _blocked_profile("ChIJ_B", "https://b"),
         "akamai challenge page"),
        # No third response scripted — if the controller didn't stop,
        # the fake would raise AssertionError.
    ])
    ctrl = EnrichPractoController(
        gw, repo, inter_lead_seconds=0, clock=lambda: _T, sleep=lambda _: None
    )

    result = ctrl.enrich_pending()

    assert result.stopped_early is True
    assert result.n_ok == 1
    assert result.n_blocked == 1
    assert result.n_attempted == 2  # A and B tried; C never reached.
    # B persists with blocked status so we can audit.
    b = repo.get_by_place_id("ChIJ_B")
    assert b.fetch_status == "blocked"
    assert b.error_message and "akamai" in b.error_message.lower()
    # C remains pending for the next run.
    c = repo.get_by_place_id("ChIJ_C")
    assert c.fetch_status == "pending"


# ── errors are recorded but don't stop the run ─────────────────────


def test_enrich_pending_continues_past_errors(repo):
    repo.upsert_stub("ChIJ_A", "https://a", now=_T)
    repo.upsert_stub("ChIJ_B", "https://b", now=_T.replace(microsecond=1))
    gw = FakePractoGateway(responses=[
        ("error", None, "playwright timeout: navigation"),
        ("ok", _ok_profile("ChIJ_B", "https://b"), None),
    ])
    ctrl = EnrichPractoController(
        gw, repo, inter_lead_seconds=0, clock=lambda: _T, sleep=lambda _: None
    )

    result = ctrl.enrich_pending()

    assert result.n_error == 1
    assert result.n_ok == 1
    assert not result.stopped_early
    a = repo.get_by_place_id("ChIJ_A")
    assert a.fetch_status == "error"
    assert a.error_message and "timeout" in a.error_message


# ── enrich_one ────────────────────────────────────────────────────


def test_enrich_one_returns_none_when_no_stub(repo):
    gw = FakePractoGateway(responses=[])
    ctrl = EnrichPractoController(
        gw, repo, inter_lead_seconds=0, clock=lambda: _T, sleep=lambda _: None
    )
    out = ctrl.enrich_one("ChIJ_NONE")
    assert out is None
    assert gw.calls == []


def test_enrich_one_force_refreshes_existing_row(repo):
    """enrich_one bypasses the pending filter — useful for refresh."""
    repo.upsert(_ok_profile("ChIJ_A", "https://a", "Dr. Old"))
    gw = FakePractoGateway(responses=[
        ("ok", _ok_profile("ChIJ_A", "https://a", "Dr. New"), None),
    ])
    ctrl = EnrichPractoController(
        gw, repo, inter_lead_seconds=0, clock=lambda: _T, sleep=lambda _: None
    )
    out = ctrl.enrich_one("ChIJ_A")
    assert out is not None
    assert out.name == "Dr. New"


# ── inter_lead sleep ──────────────────────────────────────────────


def test_inter_lead_sleep_called_between_but_not_after_last(repo):
    repo.upsert_stub("ChIJ_A", "https://a", now=_T)
    repo.upsert_stub("ChIJ_B", "https://b", now=_T.replace(microsecond=1))
    gw = FakePractoGateway(responses=[
        ("ok", _ok_profile("ChIJ_A", "https://a"), None),
        ("ok", _ok_profile("ChIJ_B", "https://b"), None),
    ])
    sleeps: list[float] = []
    ctrl = EnrichPractoController(
        gw,
        repo,
        inter_lead_seconds=2.5,
        inter_lead_jitter_seconds=0,
        clock=lambda: _T,
        sleep=sleeps.append,
    )

    ctrl.enrich_pending()

    assert sleeps == [2.5]  # one sleep between the two leads, none after the last


def test_inter_lead_sleep_includes_jitter(repo):
    """Jitter is added on top of the base delay using the injected rng."""
    repo.upsert_stub("ChIJ_A", "https://a", now=_T)
    repo.upsert_stub("ChIJ_B", "https://b", now=_T.replace(microsecond=1))
    repo.upsert_stub("ChIJ_C", "https://c", now=_T.replace(microsecond=2))
    gw = FakePractoGateway(responses=[
        ("ok", _ok_profile("ChIJ_A", "https://a"), None),
        ("ok", _ok_profile("ChIJ_B", "https://b"), None),
        ("ok", _ok_profile("ChIJ_C", "https://c"), None),
    ])
    sleeps: list[float] = []
    rng_calls: list[tuple[float, float]] = []

    def fake_rng(lo: float, hi: float) -> float:
        rng_calls.append((lo, hi))
        return 1.7  # deterministic "random" for the test

    ctrl = EnrichPractoController(
        gw,
        repo,
        inter_lead_seconds=4.0,
        inter_lead_jitter_seconds=3.0,
        clock=lambda: _T,
        sleep=sleeps.append,
        rng=fake_rng,
    )

    ctrl.enrich_pending()

    assert sleeps == [5.7, 5.7]  # 4.0 base + 1.7 jitter, twice (between 3 leads)
    assert rng_calls == [(0.0, 3.0), (0.0, 3.0)]


def test_init_rejects_negative_inter_lead_seconds(repo):
    with pytest.raises(ValueError):
        EnrichPractoController(
            gateway=FakePractoGateway(),
            repo=repo,
            inter_lead_seconds=-1,
        )


def test_init_rejects_negative_jitter(repo):
    with pytest.raises(ValueError):
        EnrichPractoController(
            gateway=FakePractoGateway(),
            repo=repo,
            inter_lead_jitter_seconds=-0.1,
        )
