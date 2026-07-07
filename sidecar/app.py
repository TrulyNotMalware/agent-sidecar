from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .codex_runner import ensure_codex_auth
from .concurrency import ConcurrencyGate
from .config import get_settings
from .errors import ErrorCode
from .inflight import InflightRegistry
from .observability.logging import configure_logging, get_logger
from .routes import cancel, converse, health, metrics

log = get_logger("sidecar.app")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(level=settings.log_level, redact=not settings.log_prompts)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.provider == "codex":
            authed = await ensure_codex_auth(settings.codex_auth_path)
            log.info("codex.auth", materialized=authed)
        app.state.gate = ConcurrencyGate(settings.max_concurrent)
        app.state.inflight = InflightRegistry()
        try:
            yield
        finally:
            forced = await app.state.inflight.drain(
                grace_sec=settings.shutdown_grace_sec
            )
            log.info("shutdown.drained", forced_cancellations=forced)

    app = FastAPI(title="Claude Sidecar", version="1.0.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(cancel.router)
    app.include_router(converse.router)

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
