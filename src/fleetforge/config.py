"""Application settings.

Deliberately tiny. `alembic/env.py` reads `DATABASE_URL` straight from the
environment and never imports this module: `content` learned the hard way that
importing app config into Alembic forces every unrelated setting to validate
before `alembic upgrade head` can run, which turns a missing OAuth secret into a
failed migration.

R0-be-1 extends this with the settings the API needs. `database_url` must stay
required — a silently-defaulted database URL is how a service ends up writing to
the wrong database.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from the environment (or a local `.env`)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, constructed on first use."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
