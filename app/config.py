"""Boot configuration.

Everything here is a *boot* setting: it comes from the environment (the .env file
written by the installer) and is required for the platform to start. Business
settings are NOT here — they live in the database and are edited from the panel.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # --- Telegram / admin bootstrap ---
    BOT_TOKEN: str = ""
    MAIN_ADMIN_TELEGRAM_ID: int = 0
    ADMIN_TELEGRAM_IDS: str = ""

    # --- Web panel ---
    DOMAIN: str = "localhost"
    WEB_PANEL_URL: str = "http://localhost:8000"
    WEB_ADMIN_USERNAME: str = "admin"
    # Consumed once by the seeder to bootstrap the owner's web password.
    WEB_ADMIN_PASSWORD: str = ""

    # --- Datastores ---
    POSTGRES_USER: str = "digitalcore"
    POSTGRES_PASSWORD: str = ""
    POSTGRES_DB: str = "digitalcore"
    DATABASE_URL: str = "postgresql+asyncpg://digitalcore:digitalcore@postgres:5432/digitalcore"
    REDIS_URL: str = "redis://redis:6379/0"

    # --- Secrets ---
    SECRET_KEY: str = "change-me"
    JWT_SECRET: str = "change-me"
    FERNET_KEY: str = ""
    BACKUP_ENCRYPTION_KEY: str = ""

    # --- Runtime flags ---
    MAINTENANCE_MODE: bool = False

    # --- JWT ---
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 60 * 12

    @field_validator("MAIN_ADMIN_TELEGRAM_ID", mode="before")
    @classmethod
    def _empty_id_to_zero(cls, v):
        if v in ("", None):
            return 0
        return v

    @property
    def admin_telegram_ids(self) -> list[int]:
        """Parsed list of admin Telegram IDs (always includes the main admin)."""
        ids: list[int] = []
        for part in self.ADMIN_TELEGRAM_IDS.split(","):
            part = part.strip()
            if part.isdigit():
                ids.append(int(part))
        if self.MAIN_ADMIN_TELEGRAM_ID and self.MAIN_ADMIN_TELEGRAM_ID not in ids:
            ids.insert(0, self.MAIN_ADMIN_TELEGRAM_ID)
        return ids

    @property
    def cookie_secure(self) -> bool:
        """Mark the session cookie Secure when the panel is served over HTTPS.

        Derived from the panel URL scheme so real (https) deployments get a
        Secure cookie while local http development still works.
        """
        return self.WEB_PANEL_URL.lower().startswith("https")

    @property
    def cookie_max_age(self) -> int:
        """Session cookie lifetime in seconds, kept in sync with the JWT expiry."""
        return self.JWT_EXPIRE_MINUTES * 60

    def insecure_config_warnings(self) -> list[str]:
        """Human-readable warnings for weak/placeholder boot secrets.

        Emitted at startup so an install that bypassed install.sh (and therefore
        never generated secrets) is loud about it instead of silently running on
        guessable keys.
        """
        warnings: list[str] = []
        if self.SECRET_KEY in ("", "change-me"):
            warnings.append("SECRET_KEY is unset/placeholder — generate a strong value.")
        if self.JWT_SECRET in ("", "change-me"):
            warnings.append("JWT_SECRET is unset/placeholder — panel tokens are forgeable.")
        if not (self.FERNET_KEY or "").strip():
            warnings.append(
                "FERNET_KEY is unset — secret settings are encrypted with a key "
                "derived from SECRET_KEY; set a dedicated FERNET_KEY."
            )
        return warnings


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
