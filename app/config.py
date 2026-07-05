"""Application configuration.

All values are read from the environment (the .env file in development). The
backend must boot even when the optional Telegram values are empty.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # --- Application ---
    PROJECT_NAME: str = "DigitalCore"
    APP_ENV: str = "development"
    APP_VERSION: str = "0.1.0"
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000

    # --- Database ---
    POSTGRES_DB: str = "digitalcore"
    POSTGRES_USER: str = "digitalcore"
    POSTGRES_PASSWORD: str = "digitalcore_password"
    DATABASE_URL: str = (
        "postgresql+asyncpg://digitalcore:digitalcore_password@postgres:5432/digitalcore"
    )

    # --- Cache ---
    REDIS_URL: str = "redis://redis:6379/0"

    # --- Secrets / crypto ---
    # SECRET_KEY seeds the Fernet fallback in app/core/crypto.py; FERNET_KEY, when
    # set, is used directly. BACKUP_ENCRYPTION_KEY and WEB_PANEL_URL are consumed
    # by later phases but are declared here so the config resolves and the app
    # boots even when they are blank.
    SECRET_KEY: str = "change_me"
    FERNET_KEY: str = ""
    BACKUP_ENCRYPTION_KEY: str = ""
    WEB_PANEL_URL: str = ""

    # --- Logging ---
    LOG_LEVEL: str = "INFO"

    # --- Auth / admin bootstrap ---
    JWT_SECRET: str = "change_me"
    ADMIN_USERNAME: str = "admin"
    # Optional: an email can be attached to the admin and also used to sign in.
    ADMIN_EMAIL: str = ""
    ADMIN_PASSWORD: str = "change_me"

    # --- Telegram (optional) ---
    # Accept both the canonical names and the shorter aliases the installer/docs
    # use, so a fresh install never fails just because of a variable-name choice:
    #   TELEGRAM_BOT_TOKEN  <- BOT_TOKEN
    #   TELEGRAM_ADMIN_ID   <- MAIN_ADMIN_TELEGRAM_ID
    # The attribute names (settings.TELEGRAM_BOT_TOKEN / .TELEGRAM_ADMIN_ID) stay
    # the same, so nothing downstream changes.
    TELEGRAM_BOT_TOKEN: str = Field(
        default="",
        validation_alias=AliasChoices("TELEGRAM_BOT_TOKEN", "BOT_TOKEN"),
    )
    TELEGRAM_ADMIN_ID: int | None = Field(
        default=None,
        validation_alias=AliasChoices("TELEGRAM_ADMIN_ID", "MAIN_ADMIN_TELEGRAM_ID"),
    )

    # --- Session cookie ---
    # "auto" (default): Secure only when the request actually arrived over HTTPS
    # (via TLS termination + X-Forwarded-Proto). "true"/"false" force it.
    COOKIE_SECURE: str = "auto"

    # --- JWT tuning (not required in .env) ---
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 60 * 12

    @field_validator("TELEGRAM_ADMIN_ID", mode="before")
    @classmethod
    def _blank_admin_id(cls, v):
        if v in ("", None):
            return None
        return v

    @property
    def service_name(self) -> str:
        """Human-facing service name used by the /health payload."""
        return f"{self.PROJECT_NAME} API"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
