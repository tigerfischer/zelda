from pathlib import Path

import pytest


_ZELDA_ENV_VARS = (
    "GOOGLE_PLACES_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_DRIVE_FOLDER_ID",
    "DATA_DIR",
    "DB_PATH",
    "RAW_ARTIFACTS_DIR",
)


@pytest.fixture(autouse=True)
def _isolate_zelda_env(monkeypatch):
    """Strip Zelda-related env vars before each test so the developer's real
    `.env` / shell never leaks into Settings construction."""
    for var in _ZELDA_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def fake_credentials_file(tmp_path: Path) -> Path:
    """A throwaway file that satisfies the GOOGLE_APPLICATION_CREDENTIALS
    file-must-exist validator."""
    cred = tmp_path / "fake-sa.json"
    cred.write_text("{}")
    return cred


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"
