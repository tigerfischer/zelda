from datetime import datetime, timezone

import pytest

from zelda.models.practo_profile import PractoProfile


_T = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)


def _stub(**overrides) -> PractoProfile:
    base = dict(
        place_id="ChIJ_X",
        practo_url="https://www.practo.com/bangalore/doctor/dr-x",
        discovered_at=_T,
        last_modified_at=_T,
    )
    base.update(overrides)
    return PractoProfile(**base)


def test_stub_defaults_status_to_pending():
    p = _stub()
    assert p.fetch_status == "pending"
    assert p.fetched_at is None
    assert p.is_pending is True
    assert p.is_enriched is False


def test_enriched_profile_is_not_pending():
    p = _stub(fetch_status="ok", fetched_at=_T, name="Dr. X")
    assert p.is_pending is False
    assert p.is_enriched is True


def test_terminal_failure_statuses_are_not_enriched():
    for status in ("not_found", "no_url_found", "blocked", "error"):
        p = _stub(fetch_status=status, fetched_at=_T)
        assert p.is_pending is False
        assert p.is_enriched is False


def test_invalid_fetch_status_rejected():
    with pytest.raises(ValueError):
        _stub(fetch_status="bogus")  # type: ignore[arg-type]


def test_unknown_fields_dropped():
    """`extra='ignore'` on the model — unknown Apify fields shouldn't
    blow up the model; raw_json is the catch-all."""
    p = _stub(some_unknown_field="whatever")
    assert not hasattr(p, "some_unknown_field")


def test_default_collections_are_independent_per_instance():
    """Pydantic Field(default_factory=list) shouldn't share state."""
    a = _stub()
    b = _stub()
    a.qualifications.append("BDS")
    assert b.qualifications == []
