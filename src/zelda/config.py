from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Loads from `.env` and the process environment."""

    google_places_api_key: str
    google_drive_folder_id: str
    anthropic_api_key: str = ""

    # Outreach pipeline (optional — only needed for Telegram bot + WhatsApp sending)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""      # your personal chat ID with the bot
    green_api_instance_id: str = ""
    green_api_token: str = ""

    # OAuth user credentials for Drive + Sheets.
    # `client_secrets` is the JSON downloaded from GCP > Credentials > OAuth client ID.
    # `token_cache` is a file we create on first auth and reuse thereafter.
    google_oauth_client_secrets: Path = Path("secrets/oauth-client.json")
    google_oauth_token_cache: Path = Path("secrets/oauth-token.json")

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

    @field_validator("google_oauth_client_secrets")
    @classmethod
    def _client_secrets_file_exists(cls, v: Path) -> Path:
        if not v.exists():
            raise ValueError(f"OAuth client secrets file not found at: {v}")
        if not v.is_file():
            raise ValueError(f"OAuth client secrets path is not a file: {v}")
        return v

    @model_validator(mode="after")
    def _resolve_derived_paths(self) -> "Settings":
        if self.db_path is None:
            self.db_path = self.data_dir / "zelda.db"
        if self.raw_artifacts_dir is None:
            self.raw_artifacts_dir = self.data_dir / "raw-artifacts"
        return self
