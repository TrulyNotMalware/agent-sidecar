import os
import shutil
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..codex_runner import codex_auth_file
from ..config import Settings, get_settings
from ..models import HealthStatus

router = APIRouter()


@router.get("/healthz", response_model=HealthStatus)
async def healthz() -> HealthStatus:
    return HealthStatus(status="ok")


@router.get("/readyz")
async def readyz() -> JSONResponse:
    settings = get_settings()
    failures: list[str] = []

    if settings.provider == "codex":
        if not _codex_binary_ready():
            failures.append("codex binary not found on PATH")
        if not _codex_identity_ready(settings):
            failures.append(
                "no codex identity (run `codex login`, or set OPENAI_API_KEY so startup "
                "materializes ~/.codex/auth.json — check the codex.auth startup log)"
            )
    else:
        if not _binary_ready():
            failures.append("claude CLI not found (neither bundled with the SDK nor on PATH)")
        if not _identity_ready(settings):
            failures.append(
                "no anthropic identity (set ANTHROPIC_API_KEY, or for local testing "
                "CLAUDE_CODE_OAUTH_TOKEN / a .claude.json in CLAUDE_CONFIG_DIR or ~)"
            )

    if failures:
        body = HealthStatus(status="error", detail="; ".join(failures)).model_dump()
        return JSONResponse(body, status_code=503)
    return JSONResponse(HealthStatus(status="ok").model_dump(), status_code=200)


def _binary_ready() -> bool:
    return _claude_cli_path() is not None


def _claude_cli_path() -> str | None:
    """The CLI the Agent SDK will run: its bundled binary first, then `claude` on PATH."""
    try:
        import claude_agent_sdk
    except ImportError:
        return shutil.which("claude")
    name = "claude.exe" if os.name == "nt" else "claude"
    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
    return str(bundled) if bundled.is_file() else shutil.which("claude")


def _identity_ready(settings: Settings) -> bool:
    """True if the claude CLI will be able to authenticate."""
    if settings.anthropic_mode == "api":
        return bool(settings.anthropic_api_key) or bool(os.environ.get("ANTHROPIC_API_KEY"))
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return True
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    # With CLAUDE_CONFIG_DIR set (as in the image) the CLI keeps .claude.json there.
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    default = Path(config_dir) / ".claude.json" if config_dir else Path.home() / ".claude.json"
    return (settings.claude_auth_path or default).exists()


def _codex_binary_ready() -> bool:
    return shutil.which("codex") is not None


# Env credentials codex reads at request time — only if passed through (allowlisted env).
# OPENAI_API_KEY is not one of them: codex only uses it via `codex login` (auth.json).
_CODEX_ENV_CREDENTIALS = ("CODEX_API_KEY",)


def _codex_identity_ready(settings: Settings) -> bool:
    """True if the codex CLI will be able to authenticate.

    codex runs with an allowlisted environment, so it authenticates from auth.json:
    written by `codex login` (subscription) or materialized from OPENAI_API_KEY at
    startup by ensure_codex_auth(). A bare OPENAI_API_KEY therefore only counts once
    that file exists — if materialization failed, the pod must not report ready. An
    env credential listed in CODEX_ENV_PASSTHROUGH also counts, since codex sees it.
    """
    if codex_auth_file(settings.codex_auth_path).exists():
        return True
    passthrough = set(settings.codex_env_passthrough_names)
    return any(os.environ.get(v) for v in _CODEX_ENV_CREDENTIALS if v in passthrough)
