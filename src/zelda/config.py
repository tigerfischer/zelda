from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Loads from `.env` and the process environment."""

    google_places_api_key: str
    google_application_credentials: Path
    google_drive_folder_id: str

    data_dir: Path = Path("data")
    db_path: Path | None = None
    raw_artifacts_dir: Path | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("google_places_api_key", "google_drive_folder_id")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must be set and non-empty")
        return v.strip()

    @field_validator("google_application_credentials")
    @classmethod
    def _credentials_file_exists(cls, v: Path) -> Path:
        if not v.exists():
            raise ValueError(f"credentials file not found at: {v}")
        if not v.is_file():
            raise ValueError(f"credentials path is not a file: {v}")
        return v

    @model_validator(mode="after")
    def _resolve_derived_paths(self) -> "Settings":
        if self.db_path is None:
            self.db_path = self.data_dir / "zelda.db"
        if self.raw_artifacts_dir is None:
            self.raw_artifacts_dir = self.data_dir / "raw-artifacts"
        return self
