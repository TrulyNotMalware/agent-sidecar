from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    provider: Literal["claude", "codex"] = "claude"

    bind: str = "127.0.0.1"
    port: int = 7300
    bearer_secret: str = Field(default="", min_length=0)

    max_concurrent: int = 8
    turn_timeout_sec: int = 90
    cancel_grace_sec: int = 5
    shutdown_grace_sec: int = 10

    anthropic_mode: Literal["subscription", "api"] = "subscription"
    anthropic_api_key: str | None = None

    workspace_root: Path = Path("/var/lib/claude-sidecar/sessions")
    claude_md_path: Path | None = None
    mcp_config_path: Path | None = None
    claude_auth_path: Path | None = None  # defaults to ~/.claude.json at check time

    # Codex (PROVIDER=codex) — auth state written by `codex login`
    codex_auth_path: Path | None = None  # defaults to ~/.codex/auth.json at check time

    log_prompts: bool = False
    log_level: str = "INFO"

    tracing_enabled: bool = False
    otel_service_name: str = "claude-sidecar"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
