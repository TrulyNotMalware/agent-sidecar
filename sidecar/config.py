from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    provider: Literal["claude", "codex"] = "claude"

    bind: str = "127.0.0.1"
    port: int = 7300
    # Required: startup fails fast instead of serving 500s on every request.
    bearer_secret: SecretStr = Field(min_length=1)

    max_concurrent: int = 8
    turn_timeout_sec: int = 90
    # >= 1: with 0, sse-starlette cancels streams at SIGTERM before a frame can be sent.
    shutdown_grace_sec: int = Field(default=10, ge=1)

    anthropic_mode: Literal["subscription", "api"] = "subscription"
    anthropic_api_key: str | None = None

    workspace_root: Path = Path("/var/lib/claude-sidecar/sessions")
    claude_md_path: Path | None = None
    mcp_config_path: Path | None = None
    mcp_server_url: str | None = None
    mcp_server_name: str = "codecompanion"
    claude_auth_path: Path | None = None  # defaults to ~/.claude.json at check time

    # Codex (PROVIDER=codex) — auth state written by `codex login`
    codex_auth_path: Path | None = None  # defaults to ~/.codex/auth.json at check time
    # Always passed as `codex exec --sandbox`, so a config.toml cannot loosen it.
    codex_sandbox: Literal["read-only", "workspace-write", "danger-full-access"] = "read-only"
    # Comma-separated env var names passed to codex on top of the built-in allowlist
    # (e.g. a custom model provider's `env_key`). Everything else is withheld.
    codex_env_passthrough: str = ""

    log_prompts: bool = False
    log_level: str = "INFO"

    tracing_enabled: bool = False
    otel_service_name: str = "claude-sidecar"

    @property
    def codex_env_passthrough_names(self) -> tuple[str, ...]:
        return tuple(n.strip() for n in self.codex_env_passthrough.split(",") if n.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
