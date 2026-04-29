from pathlib import Path

import pytest


_ZELDA_ENV_VARS = (
    "GOOGLE_PLACES_API_KEY",
    "GOOGLE_DRIVE_FOLDER_ID",
    "GOOGLE_OAUTH_CLIENT_SECRETS",
    "GOOGLE_OAUTH_TOKEN_CACHE",
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
    """A throwaway JSON file that satisfies any 'file-must-exist'
    config validator (e.g. GOOGLE_OAUTH_CLIENT_SECRETS)."""
    cred = tmp_path / "fake-creds.json"
    cred.write_text("{}")
    return cred


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"
