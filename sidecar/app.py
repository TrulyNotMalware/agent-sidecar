import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.utils import is_body_allowed_for_status_code
from opentelemetry.sdk.trace import TracerProvider
from starlette.exceptions import HTTPException as StarletteHTTPException

from .admission import Admission
from .codex_runner import ensure_codex_auth
from .config import Settings, get_settings
from .errors import ApiError, ErrorCode
from .mcp import static_mcp_server_names
from .observability.logging import configure_logging, get_logger
from .observability.redaction import register_secrets
from .observability.tracing import configure_tracing, shutdown_tracing
from .routes import cancel, converse, health, metrics

log = get_logger("sidecar.app")

# After the HTTP streams have ended on shutdown, how long to wait for turns that
# are still closing their CLI (the SDK waits up to 5s for exit, then SIGTERMs and
# waits another 5s before SIGKILL).
TURN_CLEANUP_BUDGET_SEC = 12.0
# How long shutdown waits for the span exporter to flush its last batch.
_TRACING_FLUSH_SEC = 10.0

_CREDENTIAL_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
)


def _custom_header_values() -> list[str]:
    """ANTHROPIC_CUSTOM_HEADERS is "Name: value" lines; the values may be credentials."""
    raw = os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "")
    return [line.partition(":")[2].strip() for line in raw.splitlines() if ":" in line]


def _prepare_filesystem(settings: Settings) -> None:
    """Startup work that touches the disk; runs in a thread before the first request."""
    # Fail at startup, not on the first request, if the workspace root is unusable.
    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    # A volume mounted over the image's state dir hides the directories the image
    # created, and codex refuses to run with a CODEX_HOME that does not exist.
    for var in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):
        if os.environ.get(var):
            Path(os.environ[var]).mkdir(parents=True, exist_ok=True)
    if settings.mcp_config_path is not None and not settings.mcp_config_path.is_file():
        log.warning("mcp.config_missing", path=str(settings.mcp_config_path))
    elif settings.provider == "codex" and (
        names := static_mcp_server_names(settings.mcp_config_path)
    ):
        # Static MCP servers are claude-only; codex reads its own config.toml.
        log.warning("mcp.static_config_ignored", provider="codex", servers=names)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(level=settings.log_level, redact=not settings.log_prompts)
    # Scrubbed from error frames and log lines wherever they appear (e.g. echoed back
    # in a provider's error message or in CLI stderr).
    register_secrets(
        settings.bearer_secret.get_secret_value(),
        settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None,
        *(os.environ.get(name) for name in _CREDENTIAL_ENV_VARS),
        *_custom_header_values(),
        # Passed through to codex because they are usually a custom provider's key.
        *(os.environ.get(name) for name in settings.codex_env_passthrough_names),
    )

    tracer_provider: TracerProvider | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(_prepare_filesystem, settings)
        if settings.provider == "codex":
            authed = await ensure_codex_auth(settings.codex_auth_path)
            log.info("codex.auth", materialized=authed)
        app.state.admission = Admission(settings.max_concurrent)
        try:
            yield
        finally:
            forced = await app.state.admission.drain(grace_sec=TURN_CLEANUP_BUDGET_SEC)
            log.info("shutdown.drained", forced_cancellations=forced)
            if tracer_provider is not None:
                # Bounded wait; the export thread finishes on its own either way.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        asyncio.to_thread(shutdown_tracing, tracer_provider), _TRACING_FLUSH_SEC
                    )

    app = FastAPI(title="Claude Sidecar", version="1.0.0", lifespan=lifespan)
    if settings.tracing_enabled:
        # Instrumentation must be added before the app handles its first event.
        tracer_provider = configure_tracing(app, service_name=settings.otel_service_name)
    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(cancel.router)
    app.include_router(converse.router)

    # Every error body on the wire is {"code", "message"} (openapi `Error`), whether it
    # comes from a dependency (auth), a route (cancel 404) or validation (400).
    @app.exception_handler(ApiError)
    async def _api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.code is ErrorCode.UNAUTHORIZED else None
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code.value, "message": exc.message},
            headers=headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(_request: Request, exc: StarletteHTTPException) -> Response:
        # The router's own errors (unknown path, wrong method) in the same shape. The
        # Starlette class, not FastAPI's: the router raises the base class.
        if not is_body_allowed_for_status_code(exc.status_code):
            return Response(status_code=exc.status_code, headers=exc.headers)
        if exc.status_code == 404:
            code = ErrorCode.NOT_FOUND
        elif exc.status_code < 500:
            code = ErrorCode.BAD_REQUEST
        else:
            code = ErrorCode.INTERNAL
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": code.value, "message": str(exc.detail)},
            headers=exc.headers,  # e.g. Allow on 405
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(_request: Request, exc: Exception) -> JSONResponse:
        # Last resort, for a bug before the stream opens: the server logs the traceback;
        # the client gets the shape and nothing of the exception's text.
        return JSONResponse(
            status_code=500,
            content={"code": ErrorCode.INTERNAL.value, "message": "internal error"},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        msg = "; ".join(
            f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg', '')}"
            for err in exc.errors()
        )
        return JSONResponse(
            status_code=400,
            content={"code": ErrorCode.BAD_REQUEST.value, "message": msg or "invalid request"},
        )

    return app
