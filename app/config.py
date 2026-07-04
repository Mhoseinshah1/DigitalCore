"""Application configuration.

All values are read from the environment (the .env file in development). The
backend must boot even when the optional Telegram values are empty.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
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

    # --- Auth / admin bootstrap ---
    JWT_SECRET: str = "change_me"
    ADMIN_EMAIL: str = "admin@example.com"
    ADMIN_PASSWORD: str = "change_me"

    # --- Telegram (optional) ---
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_ADMIN_ID: int | None = None

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
