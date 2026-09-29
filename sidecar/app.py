import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .codex_runner import ensure_codex_auth
from .concurrency import ConcurrencyGate
from .config import get_settings
from .errors import ApiError, ErrorCode
from .inflight import InflightRegistry
from .observability.logging import configure_logging, get_logger
from .routes import cancel, converse, health, metrics

log = get_logger("sidecar.app")

# After the HTTP streams have ended on shutdown, how long to wait for turns that
# are still closing their CLI (the SDK waits up to 5s for exit, then SIGTERMs and
# waits another 5s before SIGKILL).
TURN_CLEANUP_BUDGET_SEC = 12.0


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(level=settings.log_level, redact=not settings.log_prompts)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Fail at startup, not on the first request, if the workspace root is unusable.
        settings.workspace_root.mkdir(parents=True, exist_ok=True)
        # A volume mounted over the image's state dir hides the directories the image
        # created, and codex refuses to run with a CODEX_HOME that does not exist.
        for var in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):
            if os.environ.get(var):
                Path(os.environ[var]).mkdir(parents=True, exist_ok=True)
        if settings.mcp_config_path is not None and not settings.mcp_config_path.is_file():
            log.warning("mcp.config_missing", path=str(settings.mcp_config_path))
        if settings.provider == "codex":
            authed = await ensure_codex_auth(settings.codex_auth_path)
            log.info("codex.auth", materialized=authed)
        app.state.gate = ConcurrencyGate(settings.max_concurrent)
        app.state.inflight = InflightRegistry()
        try:
            yield
        finally:
            forced = await app.state.inflight.drain(grace_sec=TURN_CLEANUP_BUDGET_SEC)
            log.info("shutdown.drained", forced_cancellations=forced)

    app = FastAPI(title="Claude Sidecar", version="1.0.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(cancel.router)
    app.include_router(converse.router)

    # Every error body on the wire is {"code", "message"} (openapi `Error`), whether it
    # comes from a dependency (auth), a route (cancel 404) or validation (400).
    @app.exception_handler(ApiError)
    async def _api_error_handler(_request: Request, exc: ApiError):
        headers = {"WWW-Authenticate": "Bearer"} if exc.code is ErrorCode.UNAUTHORIZED else None
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code.value, "message": exc.message},
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_request: Request, exc: RequestValidationError):
        msg = "; ".join(
            f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg', '')}"
            for err in exc.errors()
        )
        return JSONResponse(
            status_code=400,
            content={"code": ErrorCode.BAD_REQUEST.value, "message": msg or "invalid request"},
        )

    if settings.tracing_enabled:
        from .observability.tracing import configure_tracing
        configure_tracing(app, service_name=settings.otel_service_name)

    return app


app = create_app()
