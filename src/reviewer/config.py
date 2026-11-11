from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def to_sqlalchemy_url(url: str) -> str:
    """Points a plain Postgres URL (as hosts like Neon give it) at the psycopg 3 driver SQLAlchemy should use."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


class Settings(BaseSettings):
    """Environment configuration. Read from the process environment, and from .env during development."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    port: int = 3001

    github_app_id: str
    # The .pem contents; a single line with literal "\n" for newlines is fine (see the validator).
    github_private_key: str
    github_webhook_secret: str

    redis_url: str = "redis://localhost:6379"

    # "fake" = offline demo model, "gemini" = Google Gemini (needs llm_api_key).
    llm_provider: str = "fake"
    llm_api_key: str | None = None
    llm_model: str | None = None

    # Without it the worker still reviews PRs but stores nothing.
    database_url: str | None = Field(default=None)

    @field_validator("github_private_key")
    @classmethod
    def _unescape_newlines(cls, v: str) -> str:
        return v.replace("\\n", "\n")

    @field_validator("llm_api_key", "llm_model", "database_url", mode="before")
    @classmethod
    def _empty_is_none(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @property
    def sqlalchemy_url(self) -> str | None:
        return to_sqlalchemy_url(self.database_url) if self.database_url else None


@lru_cache
def get_settings() -> Settings:
    return Settings()  # required fields come from the environment
