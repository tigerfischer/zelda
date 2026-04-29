from pathlib import Path

import pytest
from pydantic import ValidationError

from zelda.config import Settings


def _write_env(tmp_path: Path, content: str) -> Path:
    env = tmp_path / ".env"
    env.write_text(content)
    return env


def test_settings_loads_from_env_file(tmp_path: Path, fake_credentials_file: Path):
    env = _write_env(
        tmp_path,
        f"""\
GOOGLE_PLACES_API_KEY=test-key-123
GOOGLE_APPLICATION_CREDENTIALS={fake_credentials_file}
GOOGLE_DRIVE_FOLDER_ID=folder-abc
""",
    )
    settings = Settings(_env_file=str(env))

    assert settings.google_places_api_key == "test-key-123"
    assert settings.google_application_credentials == fake_credentials_file
    assert settings.google_drive_folder_id == "folder-abc"


def test_settings_loads_from_env_vars(monkeypatch, fake_credentials_file: Path):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "shell-key")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(fake_credentials_file))
    monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "shell-folder")

    settings = Settings(_env_file=None)

    assert settings.google_places_api_key == "shell-key"
    assert settings.google_drive_folder_id == "shell-folder"


def test_settings_fails_when_credentials_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "does-not-exist.json"))
    monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "f")

    with pytest.raises(ValidationError, match="not found"):
        Settings(_env_file=None)


def test_settings_fails_when_api_key_blank(monkeypatch, fake_credentials_file: Path):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "   ")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(fake_credentials_file))
    monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "f")

    with pytest.raises(ValidationError, match="non-empty"):
        Settings(_env_file=None)


def test_settings_derives_db_and_artifacts_paths(monkeypatch, tmp_path: Path, fake_credentials_file: Path):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(fake_credentials_file))
    monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "f")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "datadir"))

    settings = Settings(_env_file=None)

    assert settings.db_path == tmp_path / "datadir" / "zelda.db"
    assert settings.raw_artifacts_dir == tmp_path / "datadir" / "raw-artifacts"


def test_settings_explicit_db_path_override(monkeypatch, tmp_path: Path, fake_credentials_file: Path):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(fake_credentials_file))
    monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "f")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "custom.db"))

    settings = Settings(_env_file=None)

    assert settings.db_path == tmp_path / "custom.db"
