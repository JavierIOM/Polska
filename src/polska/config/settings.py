"""Secrets and environment. Nothing in this module belongs in a tracked file.

Everything here comes from the process environment, optionally seeded from a local
``.env`` that is gitignored. ``.env.example`` documents the names with empty values.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-provided configuration.

    ``database_url``, ``config_path``, ``companies_dir`` and ``workspace_root``
    default to plain paths relative to the process's current working
    directory, not to this file's own location. That is deliberate, found the
    hard way: an earlier version derived these from ``Path(__file__)``, which
    is only meaningful for an editable install (``pip install -e .``, true in
    dev and in CI) where ``__file__`` resolves inside the real source tree. A
    non-editable install -- exactly what the Docker image does with
    ``pip install ".[runtime]"`` -- copies the package into site-packages
    instead, where that same computation lands under
    ``/usr/local/lib/python3.12/`` or similar: a real path, just the wrong
    one, so it fails silently at the point something tries to read a file
    there rather than at settings-construction time. It surfaced as
    ``load_app_config`` raising ``FileNotFoundError`` for
    ``.../python3.12/config/default.yaml``, with no clue from the error alone
    that the cause was an install-mode difference three modules away.

    Relative-to-cwd is the fix, not a per-container hardcoded absolute path,
    because it is the one resolution strategy that is already correct in both
    real invocation contexts this project has: ``python -m polska.main`` (or
    ``.dashboard.server``, or ``.cli``) run from the repo root in dev, and the
    same commands run with ``WORKDIR /app`` in the container, where
    ``docker-compose.yml`` bind-mounts ``config/``, ``companies/`` and
    ``data/`` at exactly that path. Every env var below can still override
    these explicitly (see ``.env.example``) and ``docker-compose.yml``'s
    services still set them for clarity, but correctness no longer depends on
    that: the same relative default resolves right either way, with or
    without ``.env`` populated.
    """

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

    database_url: str = "sqlite+pysqlite:///data/polska.db"
    config_path: Path = Path("config/default.yaml")
    companies_dir: Path = Path("companies")
    workspace_root: Path = Path("data/workspaces")

    #: Dashboard auth. Argon2 hash, never a plaintext password.
    admin_password_hash: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("")
    session_max_age_seconds: int = 60 * 60 * 12

    #: 0.0.0.0 exposes this to the network; 127.0.0.1 requires an SSH tunnel or a
    #: reverse proxy on the same host. This is a deliberate per-deployment choice,
    #: not a code decision, which is exactly why it is a setting rather than
    #: hardcoded either way. Temporary, public-facing use is the current plan
    #: (see README): this must move behind Cloudflare Access or back to
    #: 127.0.0.1 before any adapter stops being force_dry_run.
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8000
    #: The session cookie's Secure flag: only sent back over HTTPS. False by
    #: default because this currently serves plain HTTP directly; set true the
    #: moment a TLS-terminating proxy (Cloudflare or otherwise) sits in front of
    #: it, or the cookie will still be issued but the browser will silently
    #: refuse to return it and every login will appear to fail for no visible
    #: reason.
    dashboard_cookie_secure: bool = False

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
