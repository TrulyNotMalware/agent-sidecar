import asyncio
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from ..auth import require_bearer
from ..claude_runner import (
    DoneEvent,
    SessionEvent,
    TextEvent,
    ToolResultEvent,
    ToolUseEvent,
)
from ..config import Settings, get_settings
from ..errors import ApiError, ErrorCode
from ..inflight import InflightHandle, InflightRegistry
from ..models import ConverseRequest
from ..observability.logging import get_logger
from ..observability.metrics import INFLIGHT, REQUEST_DURATION, REQUESTS, TOKENS, TOOL_CALLS
from ..observability.tracing import get_tracer
from ..session import stateless_workspace, workspace_for
from ..sse import sse_event

router = APIRouter()
log = get_logger("sidecar.converse")
tracer = get_tracer("sidecar.converse")


@router.post("/v1/converse", dependencies=[Depends(require_bearer)], response_model=None)
async def converse(
    body: ConverseRequest,
    request: Request,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    x_turn_token: Annotated[str | None, Header(alias="X-Turn-Token")] = None,
) -> EventSourceResponse | JSONResponse:
    settings = get_settings()
    gate = request.app.state.gate
    registry: InflightRegistry = request.app.state.inflight

    log.info(
        "converse.start",
        session_key=body.session_key,
        user_id=x_user_id,
        mode=body.mode,
        resume=bool(body.session_id),
        turn_token=bool(x_turn_token),
        prompt=body.prompt,
    )

    # Preflight: reject over-limit requests with a real HTTP 429 while the
    # status line can still carry it. The stream path re-checks atomically on
    # registration/acquire; a limit hit only in that race window still arrives
    # as a terminal SSE `error` frame with code=busy.
    busy = await _preflight_busy(body, x_user_id, gate, registry)
    if busy is not None:
        log.warning("converse.reject", code=busy.code.value, message=busy.message)
        REQUESTS.labels(outcome=busy.code.value).inc()
        REQUEST_DURATION.labels(outcome=busy.code.value).observe(0.0)
        return JSONResponse(
            status_code=busy.status_code,
            content={"code": busy.code.value, "message": busy.message},
        )

    return EventSourceResponse(
        _event_stream(body, x_user_id, x_turn_token, settings, gate, registry),
        ping=15,
    )


async def _preflight_busy(
    body: ConverseRequest,
    x_user_id: str | None,
    gate,
    registry: InflightRegistry,
) -> ApiError | None:
    if await registry.get(body.session_key) is not None:
        return ApiError(
            ErrorCode.BUSY, f"sessionKey {body.session_key!r} already in-flight"
        )
    try:
        await gate.check(
            user_id=x_user_id,
            session_key=body.session_key if body.mode == "session" else None,
        )
    except ApiError as exc:
        return exc
    return None


async def _event_stream(
    body: ConverseRequest,
    x_user_id: str | None,
    x_turn_token: str | None,
    settings: Settings,
    gate,
    registry: InflightRegistry,
):
    started = time.perf_counter()
    outcome = "ok"
    cancel_event = asyncio.Event()

    handle = InflightHandle(
        session_key=body.session_key,
        user_id=x_user_id,
        cancel_event=cancel_event,
        task=asyncio.current_task(),
    )

    span_cm = tracer.start_as_current_span("claude.turn")
    workspace_cm = (
        stateless_workspace() if body.mode == "stateless"
        else nullcontext(workspace_for(body.session_key, root=settings.workspace_root))
    )

    try:
        await registry.register(handle)
    except ApiError as exc:
        outcome = exc.code.value
        log.warning("converse.reject", code=exc.code.value, message=exc.message)
        yield sse_event("error", {"code": exc.code.value, "message": exc.message})
        REQUESTS.labels(outcome=outcome).inc()
        REQUEST_DURATION.labels(outcome=outcome).observe(time.perf_counter() - started)
        return

    try:
        with span_cm as span:
            _set_span_attrs(span, body, x_user_id)
            with workspace_cm as cwd:
                merged_system = _merge_system_prompt(
                    base_path=settings.claude_md_path,
                    system_prompt=body.system_prompt,
                    append_system_prompt=body.append_system_prompt,
                )
                async with gate.acquire(
                    user_id=x_user_id,
                    session_key=body.session_key if body.mode == "session" else None,
                ):
                    INFLIGHT.inc()
                    run_turn = _get_runner(settings)
                    try:
                        async for ev in run_turn(
                            prompt=body.prompt,
                            cwd=cwd,
                            system_prompt=merged_system,
                            resume_session_id=body.session_id,
                            mcp_config_path=settings.mcp_config_path,
                            mcp_server_url=settings.mcp_server_url,
                            mcp_server_name=settings.mcp_server_name,
                            turn_token=x_turn_token,
                            timeout_sec=settings.turn_timeout_sec,
                        ):
                            if cancel_event.is_set():
                                outcome = ErrorCode.CANCELLED.value
                                yield sse_event("error", {
                                    "code": outcome,
                                    "message": "turn cancelled by client",
                                })
                                return
                            _instrument(ev, span)
                            yield _to_sse(ev)
                    finally:
                        INFLIGHT.dec()
            _record_outcome_attr(span, outcome)
    except ApiError as exc:
        outcome = exc.code.value
        log.warning("converse.error", code=exc.code.value, message=exc.message)
        yield sse_event("error", {"code": exc.code.value, "message": exc.message})
    except asyncio.CancelledError:
        outcome = ErrorCode.CANCELLED.value
        log.info("converse.force_cancelled", session_key=body.session_key)
        raise
    except Exception as exc:  # noqa: BLE001
        outcome = ErrorCode.INTERNAL.value
        log.error("converse.unhandled", exc_type=type(exc).__name__)
        yield sse_event("error", {
            "code": outcome,
            "message": f"{type(exc).__name__}: {exc}",
        })
    finally:
        await registry.unregister(body.session_key, handle)
        REQUESTS.labels(outcome=outcome).inc()
        REQUEST_DURATION.labels(outcome=outcome).observe(time.perf_counter() - started)
        log.info(
            "converse.finish",
            session_key=body.session_key,
            outcome=outcome,
            duration_seconds=round(time.perf_counter() - started, 3),
        )


def _set_span_attrs(span, body: ConverseRequest, user_id: str | None) -> None:
    span.set_attribute("session.key", body.session_key)
    span.set_attribute("session.mode", body.mode)
    span.set_attribute("session.resume", bool(body.session_id))
    if user_id:
        span.set_attribute("user.id", user_id)


def _record_outcome_attr(span, outcome: str) -> None:
    span.set_attribute("outcome", outcome)


def _instrument(ev, span) -> None:
    if isinstance(ev, ToolUseEvent):
        TOOL_CALLS.labels(tool_name=ev.name, outcome="started").inc()
        log.info("converse.tool_use", tool_name=ev.name, args=ev.args)
        span.add_event("tool_use", {"name": ev.name})
    elif isinstance(ev, ToolResultEvent):
        TOOL_CALLS.labels(tool_name=ev.name, outcome="ok" if ev.ok else "error").inc()
        log.info("converse.tool_result", tool_name=ev.name, ok=ev.ok)
        span.add_event("tool_result", {"name": ev.name, "ok": ev.ok})
    elif isinstance(ev, DoneEvent):
        TOKENS.labels(kind="input").inc(ev.input_tokens)
        TOKENS.labels(kind="output").inc(ev.output_tokens)
        span.set_attribute("tokens.input", ev.input_tokens)
        span.set_attribute("tokens.output", ev.output_tokens)


def _merge_system_prompt(
    *,
    base_path: Path | None,
    system_prompt: str | None,
    append_system_prompt: str | None,
) -> str | None:
    base = ""
    if base_path is not None and base_path.exists():
        base = base_path.read_text(encoding="utf-8")
    if system_prompt is not None:
        return system_prompt
    if append_system_prompt is not None:
        return f"{base}\n\n{append_system_prompt}".strip() or None
    return base or None


def _get_runner(settings: Settings):
    if settings.provider == "codex":
        from ..codex_runner import run_turn
    else:
        from ..claude_runner import run_turn
    return run_turn


def _to_sse(ev) -> dict[str, str]:
    if isinstance(ev, SessionEvent):
        return sse_event("session", {"sessionId": ev.session_id})
    if isinstance(ev, TextEvent):
        return sse_event("text", {"delta": ev.delta})
    if isinstance(ev, ToolUseEvent):
        return sse_event("tool_use", {
            "name": ev.name,
            "args": ev.args,
            "toolUseId": ev.tool_use_id,
        })
    if isinstance(ev, ToolResultEvent):
        return sse_event("tool_result", {
            "name": ev.name,
            "ok": ev.ok,
            "toolUseId": ev.tool_use_id,
        })
    if isinstance(ev, DoneEvent):
        return sse_event("done", {
            "finalText": ev.final_text,
            "usage": {
                "inputTokens": ev.input_tokens,
                "outputTokens": ev.output_tokens,
                "cacheReadInputTokens": ev.cache_read_input_tokens,
                "cacheCreationInputTokens": ev.cache_creation_input_tokens,
            },
        })
    raise TypeError(f"unknown runner event: {type(ev).__name__}")
