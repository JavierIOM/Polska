"""Secrets and environment. Nothing in this module belongs in a tracked file.

Everything here comes from the process environment, optionally seeded from a local
``.env`` that is gitignored. ``.env.example`` documents the names with empty values.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Environment-provided configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="POLSKA_",
        extra="ignore",
    )

    #: Read without the POLSKA_ prefix: the Claude Agent SDK expects this exact name.
    anthropic_api_key: SecretStr = Field(
        default=SecretStr(""), validation_alias="ANTHROPIC_API_KEY"
    )

    database_url: str = f"sqlite+pysqlite:///{(REPO_ROOT / 'data' / 'polska.db').as_posix()}"
    config_path: Path = REPO_ROOT / "config" / "default.yaml"
    companies_dir: Path = REPO_ROOT / "companies"
    workspace_root: Path = REPO_ROOT / "data" / "workspaces"

    #: Dashboard auth. Argon2 hash, never a plaintext password.
    admin_password_hash: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("")
    session_max_age_seconds: int = 60 * 60 * 12

    log_level: str = "INFO"
    sql_echo: bool = False

    #: Set true only in tests and local pokes. Guards destructive helpers.
    dev_mode: bool = False

    @field_validator("log_level")
    @classmethod
    def _known_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return upper

    def require_api_key(self) -> str:
        """The Anthropic key, or a clear failure.

        Called at the point an agent is about to run rather than at import, so the
        schema and dashboard work without a key present.
        """
        key = self.anthropic_api_key.get_secret_value()
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it "
                "in, or export it in the environment."
            )
        return key

    def require_dashboard_secrets(self) -> tuple[str, str]:
        """The admin hash and session secret, or a clear failure."""
        hashed = self.admin_password_hash.get_secret_value()
        secret = self.session_secret.get_secret_value()
        missing = [
            name
            for name, value in (
                ("POLSKA_ADMIN_PASSWORD_HASH", hashed),
                ("POLSKA_SESSION_SECRET", secret),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"Dashboard cannot start without {', '.join(missing)}. "
                "Generate them with: python -m polska.cli init-auth"
            )
        return hashed, secret


def load_settings() -> Settings:
    """Build settings from the environment."""
    return Settings()
