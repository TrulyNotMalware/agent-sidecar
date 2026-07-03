import os
import shutil
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

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
                "no codex identity (run `codex login` to create ~/.codex/auth.json, "
                "or set OPENAI_API_KEY)"
            )
    else:
        if not _binary_ready():
            failures.append("claude binary not found on PATH")
        if not _identity_ready(settings):
            failures.append(
                "no anthropic identity (set CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_API_KEY, "
                "or mount ~/.claude.json)"
            )

    if failures:
        body = HealthStatus(status="error", detail="; ".join(failures)).model_dump()
        return JSONResponse(body, status_code=503)
    return JSONResponse(HealthStatus(status="ok").model_dump(), status_code=200)


def _binary_ready() -> bool:
    return shutil.which("claude") is not None


def _identity_ready(settings: Settings) -> bool:
    """True if the claude CLI will be able to authenticate."""
    if settings.anthropic_mode == "api":
        return bool(settings.anthropic_api_key) or bool(os.environ.get("ANTHROPIC_API_KEY"))
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return True
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    auth_path = settings.claude_auth_path or (Path.home() / ".claude.json")
    return auth_path.exists()


def _codex_binary_ready() -> bool:
    return shutil.which("codex") is not None


def _codex_identity_ready(settings: Settings) -> bool:
    """True if the codex CLI will be able to authenticate.

    Codex accepts an OPENAI_API_KEY env var (API key mode) or an OAuth state
    file written by `codex login` at ~/.codex/auth.json (subscription mode).
    """
    if os.environ.get("OPENAI_API_KEY"):
        return True
    auth_path = settings.codex_auth_path or (Path.home() / ".codex" / "auth.json")
    return auth_path.exists()
