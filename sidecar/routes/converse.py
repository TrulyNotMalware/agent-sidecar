import asyncio
import contextlib
import functools
import time
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from ..auth import require_bearer
from ..claude_runner import (
    DoneEvent,
    RunnerEvent,
    SessionEvent,
    TextEvent,
    ToolResultEvent,
    ToolUseEvent,
)
from ..config import Settings, get_settings
from ..errors import ApiError, ErrorCode
from ..inflight import InflightRegistry
from ..models import ConverseRequest
from ..observability.logging import get_logger
from ..observability.metrics import REQUEST_DURATION, REQUESTS
from ..session import (
    known_session_ids,
    remember_session_id,
    stateless_workspace,
    workspace_for,
)
from ..sse import sse_event
from ..turn import StopReason, Turn, TurnEnded, TurnStopped

router = APIRouter()
log = get_logger("sidecar.converse")

_SEND_TIMEOUT_SEC = 30


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
    # status line can still carry it. The turn re-checks atomically when it
    # reserves; a limit hit only in that race window still arrives as a
    # terminal SSE `error` frame with code=busy.
    rejected = _preflight_resume(body, settings) or await _preflight_busy(
        body, x_user_id, gate, registry
    )
    if rejected is not None:
        log.warning("converse.reject", code=rejected.code.value, message=rejected.message)
        REQUESTS.labels(outcome=rejected.code.value).inc()
        REQUEST_DURATION.labels(outcome=rejected.code.value).observe(0.0)
        return JSONResponse(
            status_code=rejected.status_code,
            content={"code": rejected.code.value, "message": rejected.message},
        )

    span_attributes: dict[str, str | bool] = {
        "session.key": body.session_key,
        "session.mode": body.mode,
        "session.resume": bool(body.session_id),
    }
    if x_user_id:
        span_attributes["user.id"] = x_user_id
    turn = Turn(
        session_key=body.session_key,
        user_id=x_user_id,
        gate_session_key=body.session_key if body.mode == "session" else None,
        gate=gate,
        registry=registry,
        timeout_sec=settings.turn_timeout_sec,
        span_attributes=span_attributes,
    )
    run_turn = _get_runner(settings)

    def open_runner(cwd: Path) -> AsyncIterator[RunnerEvent]:
        events = run_turn(
            prompt=body.prompt,
            cwd=cwd,
            system_prompt=_merge_system_prompt(
                base_path=settings.claude_md_path,
                system_prompt=body.system_prompt,
                append_system_prompt=body.append_system_prompt,
            ),
            resume_session_id=body.session_id,
            mcp_config_path=settings.mcp_config_path,
            mcp_server_url=settings.mcp_server_url,
            mcp_server_name=settings.mcp_server_name,
            turn_token=x_turn_token,
            ephemeral=body.mode == "stateless",
        )
        if body.mode == "stateless":
            return events
        return _remembering_session_ids(events, body.session_key, settings.workspace_root)

    def workspace() -> AbstractContextManager[Path]:
        if body.mode == "stateless":
            return stateless_workspace()
        return nullcontext(workspace_for(body.session_key, root=settings.workspace_root))

    state = _StreamState()

    async def on_client_close(_message) -> None:
        # After `done` the client leaving is expected; let the CLI wind down on its own.
        if not state.done_delivered:
            turn.stop("disconnected")

    # On SIGTERM sse-starlette sets `shutdown` and keeps the stream alive for the
    # grace period; the stream uses it to let the turn finish or end it cleanly.
    shutdown = anyio.Event()
    return EventSourceResponse(
        _event_stream(turn, open_runner, workspace, shutdown, settings, body.session_key, state),
        ping=15,
        # A client that stops reading must not hold the connection forever.
        send_timeout=_SEND_TIMEOUT_SEC,
        shutdown_event=shutdown,
        shutdown_grace_period=settings.shutdown_grace_sec,
        client_close_handler_callable=on_client_close,
    )


def _preflight_resume(body: ConverseRequest, settings: Settings) -> ApiError | None:
    """codex resolves a thread id across *all* sessions in CODEX_HOME, so a resume is
    only allowed for ids this sessionKey was issued. (claude scopes transcripts by the
    workspace cwd, which is already per-sessionKey.)"""
    if settings.provider != "codex" or body.session_id is None:
        return None
    known = known_session_ids(body.session_key, root=settings.workspace_root)
    if body.session_id not in known:
        return ApiError(ErrorCode.BAD_REQUEST, "sessionId was not issued for this sessionKey")
    return None


async def _remembering_session_ids(
    events: AsyncIterator[RunnerEvent], session_key: str, root: Path
) -> AsyncIterator[RunnerEvent]:
    async with contextlib.aclosing(events) as runner:
        async for ev in runner:
            if isinstance(ev, SessionEvent):
                remember_session_id(session_key, ev.session_id, root=root)
            yield ev


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
    turn: Turn,
    open_runner,
    workspace,
    shutdown: anyio.Event,
    settings: Settings,
    session_key: str,
    state: "_StreamState",
):
    """Relay the turn's queue as SSE: session → events → exactly one `done` | `error`.

    Runs inside sse-starlette's task group, so it never awaits cleanup: every exit
    path only *signals* the turn (Turn.stop), whose own task closes the runner.
    """
    started = time.perf_counter()
    loop = asyncio.get_running_loop()
    drain_budget = max(0.0, settings.shutdown_grace_sec - 1.0)
    drain_deadline: float | None = None
    shutdown_wait = asyncio.ensure_future(shutdown.wait())
    get: asyncio.Future | None = None
    outcome: str | None = None  # "ok" or the error code of the terminal frame sent
    turn.start(open_runner, workspace)
    # After `done` the stream stays open, sending nothing, until TurnEnded: the CLI has
    # exited and the sessionKey / user slot are free again. End of stream therefore
    # means "a new turn on this sessionKey will be accepted" (no 429 window).
    try:
        while True:
            get = asyncio.ensure_future(turn.events.get())
            if drain_deadline is None:
                done, _ = await asyncio.wait(
                    {get, shutdown_wait}, return_when=asyncio.FIRST_COMPLETED
                )
            else:
                done, _ = await asyncio.wait({get}, timeout=drain_deadline - loop.time())
            if get not in done:
                get.cancel()
                if outcome == "ok":
                    return  # shutting down while the CLI winds down; drain() covers it
                if drain_deadline is None:
                    # Shutting down: give the turn most of the grace period to finish.
                    drain_deadline = loop.time() + drain_budget
                    log.info("converse.draining", session_key=session_key)
                else:
                    turn.stop("shutdown")  # queues TurnStopped, read on the next pass
                continue

            item = get.result()
            if outcome == "ok":
                if isinstance(item, TurnEnded):
                    return
                continue  # never a second terminal: the turn logs what happens after done
            if isinstance(item, TurnStopped | TurnEnded):
                code, message = _terminal_error(item, settings.turn_timeout_sec)
                outcome = code
                log.warning("converse.error", code=code, message=message)
                yield sse_event("error", {"code": code, "message": message})
                return
            yield _to_sse(item)
            if isinstance(item, DoneEvent):
                outcome = "ok"
                state.done_delivered = True
    finally:
        # No awaits here: this may run inside an already-cancelled task group.
        shutdown_wait.cancel()
        if get is not None:
            get.cancel()
        if outcome != "ok":
            turn.stop("disconnected")  # no-op if the turn already stopped or ended
        outcome = outcome or ErrorCode.CANCELLED.value
        REQUESTS.labels(outcome=outcome).inc()
        REQUEST_DURATION.labels(outcome=outcome).observe(time.perf_counter() - started)
        log.info(
            "converse.finish",
            session_key=session_key,
            outcome=outcome,
            duration_seconds=round(time.perf_counter() - started, 3),
        )


@dataclass
class _StreamState:
    done_delivered: bool = False


_STOP_ERRORS: dict[StopReason, tuple[ErrorCode, str]] = {
    "timeout": (ErrorCode.TIMEOUT, "turn exceeded {timeout}s"),
    "cancelled": (ErrorCode.CANCELLED, "turn cancelled by client"),
    "shutdown": (ErrorCode.CANCELLED, "sidecar is shutting down"),
    "disconnected": (ErrorCode.CANCELLED, "client disconnected"),
}


def _terminal_error(item: TurnStopped | TurnEnded, timeout_sec: float) -> tuple[str, str]:
    reason = item.reason if isinstance(item, TurnStopped) else item.stop_reason
    if reason is not None:
        code, template = _STOP_ERRORS[reason]
        return code.value, template.format(timeout=timeout_sec)
    error = item.error  # TurnEnded without a stop: the runner finished or failed
    if isinstance(error, ApiError):
        return error.code.value, error.message
    if error is None:
        return ErrorCode.SDK_ERROR.value, "runner ended without a result"
    if isinstance(error, asyncio.CancelledError):
        return ErrorCode.CANCELLED.value, "turn cancelled"
    return ErrorCode.INTERNAL.value, f"{type(error).__name__}: {error}"


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

        return functools.partial(
            run_turn,
            sandbox=settings.codex_sandbox,
            env_passthrough=settings.codex_env_passthrough_names,
        )
    from ..claude_runner import run_turn

    return functools.partial(
        run_turn,
        # Anything the operator routed to codex is none of the claude agent's business.
        withheld_env=settings.codex_env_passthrough_names,
        tools=settings.claude_tools_list,
        allowed_tools=settings.claude_allowed_tools_names,
        disallowed_tools=settings.claude_disallowed_tools_names,
        permission_mode=settings.claude_permission_mode,
        setting_sources=settings.claude_setting_sources_names,
    )


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
