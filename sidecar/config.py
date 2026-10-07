from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"]
CodexSandbox = Literal["read-only", "workspace-write", "danger-full-access"]


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
    anthropic_api_key: SecretStr | None = None

    workspace_root: Path = Path("/var/lib/claude-sidecar/sessions")
    claude_md_path: Path | None = None
    mcp_config_path: Path | None = None
    mcp_server_url: str | None = None
    # Name of the per-turn MCP server entry; its tools appear as mcp__<name>__<tool>.
    mcp_server_name: str = Field(default="domain-tools", pattern=r"^[A-Za-z0-9_-]+$")
    claude_auth_path: Path | None = None  # defaults to ~/.claude.json at check time

    # Claude agent policy (PROVIDER=claude). CLAUDE_TOOLS unset keeps the CLI's default
    # built-in toolset; "" disables all built-ins; otherwise a comma-separated list.
    claude_tools: str | None = None
    # Comma-separated tools pre-approved on top of the configured MCP servers, e.g.
    # "WebFetch,Bash(git status:*)". Under dontAsk everything else that would prompt is denied.
    claude_allowed_tools: str = ""
    claude_permission_mode: PermissionMode = "dontAsk"
    # Comma-separated tools denied even if allowed elsewhere, e.g. one destructive tool
    # of an otherwise pre-approved MCP server ("mcp__domain-tools__delete_all").
    claude_disallowed_tools: str = ""
    # Comma-separated setting sources to load (user, project, local); empty = none.
    claude_setting_sources: str = ""
    # CLI --restricted: no code-running tools or WebFetch unless CLAUDE_TOOLS names them,
    # file tools confined to the workspace, bypassPermissions refused. Opt-in.
    claude_restricted: bool = False

    # Codex (PROVIDER=codex) — auth state written by `codex login`
    codex_auth_path: Path | None = None  # defaults to ~/.codex/auth.json at check time
    # Always passed as `codex exec --sandbox`, so a config.toml cannot loosen it.
    codex_sandbox: CodexSandbox = "read-only"
    # Comma-separated env var names passed to codex on top of the built-in allowlist
    # (e.g. a custom model provider's `env_key`). Everything else is withheld.
    codex_env_passthrough: str = ""

    log_prompts: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    tracing_enabled: bool = False
    otel_service_name: str = "claude-sidecar"

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, v: object) -> object:
        # LOG_LEVEL=info used to crash logging setup at import (logging wants "INFO").
        return v.upper() if isinstance(v, str) else v

    @field_validator("bearer_secret", mode="before")
    @classmethod
    def _strip_bearer_secret(cls, v: object) -> object:
        # A k8s Secret made with `echo` / --from-file often ends in "\n"; the incoming
        # token is stripped, so an unstripped secret would 401 every request.
        return v.strip() if isinstance(v, str) else v

    @field_validator("claude_setting_sources")
    @classmethod
    def _known_setting_sources(cls, v: str) -> str:
        # A typo would otherwise fail every turn ("Invalid setting source") at runtime.
        unknown = set(_csv(v)) - {"user", "project", "local"}
        if unknown:
            raise ValueError(f"unknown setting source(s): {', '.join(sorted(unknown))}")
        return v

    @property
    def codex_env_passthrough_names(self) -> tuple[str, ...]:
        return _csv(self.codex_env_passthrough)

    @property
    def claude_tools_names(self) -> tuple[str, ...] | None:
        return None if self.claude_tools is None else _csv(self.claude_tools)

    @property
    def claude_allowed_tools_names(self) -> tuple[str, ...]:
        return _tool_rules(self.claude_allowed_tools)

    @property
    def claude_disallowed_tools_names(self) -> tuple[str, ...]:
        return _tool_rules(self.claude_disallowed_tools)

    @property
    def claude_setting_sources_names(self) -> tuple[str, ...]:
        return _csv(self.claude_setting_sources)


def _csv(value: str) -> tuple[str, ...]:
    return tuple(n.strip() for n in value.split(",") if n.strip())


def _tool_rules(value: str) -> tuple[str, ...]:
    """Split permission rules on commas outside parentheses: `Bash(git log -a,b:*)` stays one."""
    rules: list[str] = []
    depth = 0
    current = ""
    for ch in value:
        if ch == "," and depth == 0:
            rules.append(current)
            current = ""
            continue
        depth += {"(": 1, ")": -1}.get(ch, 0)
        current += ch
    rules.append(current)
    return tuple(r.strip() for r in rules if r.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
