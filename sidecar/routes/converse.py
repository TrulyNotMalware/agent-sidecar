import asyncio
import contextlib
import functools
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import anyio
import structlog
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse
from starlette.types import Message

from .. import claude_runner, codex_runner
from ..admission import Admission
from ..auth import require_bearer
from ..config import Settings
from ..deps import SettingsDep
from ..errors import ApiError, ErrorCode
from ..events import (
    DoneEvent,
    Runner,
    RunnerEvent,
    SessionEvent,
    TextEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnSpec,
)
from ..models import ConverseRequest
from ..observability.logging import get_logger
from ..observability.metrics import REQUEST_DURATION, REQUESTS
from ..observability.redaction import register_turn_secret
from ..session import (
    known_session_ids,
    remember_session_id,
    stateless_workspace,
    workspace_for,
)
from ..sse import sse_event
from ..turn import StopReason, Turn, TurnEnded, TurnItem, TurnStopped

router = APIRouter()
log = get_logger("sidecar.converse")

_SEND_TIMEOUT_SEC = 30


@router.post("/v1/converse", dependencies=[Depends(require_bearer)], response_model=None)
async def converse(
    body: ConverseRequest,
    request: Request,
    settings: SettingsDep,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id", max_length=256)] = None,
    x_turn_token: Annotated[str | None, Header(alias="X-Turn-Token", max_length=4096)] = None,
) -> EventSourceResponse | JSONResponse:
    admission: Admission = request.app.state.admission
    turn_id = uuid.uuid4().hex
    # Every log line of this request — route, stream and the turn's task (created
    # below, so it inherits this context) — carries the id the client gets. Cleared
    # first: a server runs each request in a fresh task, an in-process test client
    # may not.
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(turn_id=turn_id)
    register_turn_secret(x_turn_token)  # e.g. echoed in an MCP error or CLI stderr

    log.info(
        "converse.start",
        session_key=body.session_key,
        user_id=x_user_id,
        mode=body.mode,
        resume=bool(body.session_id),
        turn_token=bool(x_turn_token),
        prompt=body.prompt,
    )

    rejected = await _preflight_resume(body, settings)
    if rejected is not None:
        return _reject(rejected, turn_id)
    try:
        # CLAUDE.md is re-read on every request (hot reload), off the event loop.
        system_prompt = await asyncio.to_thread(
            _merge_system_prompt,
            base_path=settings.claude_md_path,
            system_prompt=body.system_prompt,
            append_system_prompt=body.append_system_prompt,
        )
    except (OSError, UnicodeDecodeError) as exc:
        log.exception("system_prompt.unreadable", error_type=type(exc).__name__)
        return _reject(
            ApiError(ErrorCode.INTERNAL, "could not read the base system prompt"), turn_id
        )

    span_attributes: dict[str, str | bool] = {
        "session.key": body.session_key,
        "session.mode": body.mode,
        "session.resume": bool(body.session_id),
        "turn.id": turn_id,
    }
    if x_user_id:
        span_attributes["user.id"] = x_user_id
    turn = Turn(
        session_key=body.session_key,
        user_id=x_user_id,
        admission=admission,
        timeout_sec=settings.turn_timeout_sec,
        span_attributes=span_attributes,
        turn_id=turn_id,
    )
    run_turn = _get_runner(settings)

    def open_runner(cwd: Path) -> AsyncGenerator[RunnerEvent, None]:
        spec = TurnSpec(
            prompt=body.prompt,
            system_prompt=system_prompt,
            resume_session_id=body.session_id,
            mcp_config_path=settings.mcp_config_path,
            mcp_server_url=settings.mcp_server_url,
            mcp_server_name=settings.mcp_server_name,
            turn_token=x_turn_token,
            ephemeral=body.mode == "stateless",
        )
        events = run_turn(spec, cwd=cwd)
        if body.mode == "stateless" or settings.provider != "codex":
            return events  # only codex resumes are bound to recorded ids
        return _remembering_session_ids(events, body.session_key, settings.workspace_root)

    @asynccontextmanager
    async def workspace() -> AsyncIterator[Path]:
        if body.mode == "stateless":
            # Under WORKSPACE_ROOT (a volume in k8s), not the container's /tmp.
            async with stateless_workspace(parent=settings.workspace_root / ".stateless") as ws:
                yield ws
        else:
            root = settings.workspace_root
            yield await asyncio.to_thread(workspace_for, body.session_key, root=root)

    state = _StreamState()

    async def on_client_close(_message: Message) -> None:
        # After `done` the client leaving is expected; let the CLI wind down on its own.
        if not state.done_delivered:
            turn.stop("disconnected")

    # On SIGTERM sse-starlette sets `shutdown` and keeps the stream alive for the
    # grace period; the stream uses it to let the turn finish or end it cleanly.
    shutdown = anyio.Event()
    response = EventSourceResponse(
        _event_stream(turn, shutdown, settings, body.session_key, state),
        ping=15,
        # A client that stops reading must not hold the connection forever.
        send_timeout=_SEND_TIMEOUT_SEC,
        shutdown_event=shutdown,
        shutdown_grace_period=settings.shutdown_grace_sec,
        client_close_handler_callable=on_client_close,
        headers={"X-Turn-Id": turn_id},
    )
    # Last step before returning: reserving before the stream opens makes every limit a
    # real HTTP 429, and nothing can fail between the reservation and the response
    # (whose stream or close handler then always stops the turn).
    try:
        turn.start(open_runner, workspace)
    except ApiError as exc:
        return _reject(exc, turn_id)
    return response


async def _preflight_resume(body: ConverseRequest, settings: Settings) -> ApiError | None:
    """codex resolves a thread id across *all* sessions in CODEX_HOME, so a resume is
    only allowed for ids this sessionKey was issued. (claude scopes transcripts by the
    workspace cwd, which is already per-sessionKey.)"""
    if settings.provider != "codex" or body.session_id is None:
        return None
    try:
        known = await asyncio.to_thread(
            known_session_ids, body.session_key, root=settings.workspace_root
        )
    except (OSError, UnicodeDecodeError) as exc:
        log.exception("session_id.record_unreadable", error_type=type(exc).__name__)
        return ApiError(ErrorCode.INTERNAL, "could not read this sessionKey's session record")
    if body.session_id not in known:
        return ApiError(ErrorCode.BAD_REQUEST, "sessionId was not issued for this sessionKey")
    return None


async def _remembering_session_ids(
    events: AsyncGenerator[RunnerEvent, None], session_key: str, root: Path
) -> AsyncGenerator[RunnerEvent, None]:
    async with contextlib.aclosing(events) as runner:
        async for ev in runner:
            if isinstance(ev, SessionEvent):
                try:
                    await asyncio.to_thread(
                        remember_session_id, session_key, ev.session_id, root=root
                    )
                except OSError as exc:
                    # The turn itself is fine; only a later resume of this id gets 400.
                    log.warning("session_id.not_recorded", error_type=type(exc).__name__)
            yield ev


def _reject(error: ApiError, turn_id: str) -> JSONResponse:
    """A pre-stream error: its real HTTP status and the {code, message} body."""
    log.warning("converse.reject", code=error.code.value, message=error.message)
    REQUESTS.labels(outcome=error.code.value).inc()
    return JSONResponse(
        status_code=error.status_code,
        content={"code": error.code.value, "message": error.message},
        headers={"X-Turn-Id": turn_id},
    )


async def _event_stream(
    turn: Turn,
    shutdown: anyio.Event,
    settings: Settings,
    session_key: str,
    state: "_StreamState",
) -> AsyncIterator[dict[str, str]]:
    """Relay the turn's queue as SSE: session → events → exactly one `done` | `error`.

    Runs inside sse-starlette's task group, so it never awaits cleanup: every exit
    path only *signals* the turn (Turn.stop), whose own task closes the runner.
    """
    started = time.perf_counter()
    loop = asyncio.get_running_loop()
    drain_budget = max(0.0, settings.shutdown_grace_sec - 1.0)
    drain_deadline: float | None = None
    shutdown_wait = asyncio.ensure_future(shutdown.wait())
    get: asyncio.Future[TurnItem] | None = None
    outcome: str | None = None  # "ok" or the error code of the terminal frame sent
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
                code, message = _terminal_error(item, settings.turn_timeout_sec, turn.turn_id)
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


def _terminal_error(
    item: TurnStopped | TurnEnded, timeout_sec: float, turn_id: str
) -> tuple[str, str]:
    reason = item.reason if isinstance(item, TurnStopped) else item.stop_reason
    if reason is not None:
        code, template = _STOP_ERRORS[reason]
        return code.value, template.format(timeout=timeout_sec)
    assert isinstance(item, TurnEnded)  # a TurnStopped always carries a reason
    error = item.error  # TurnEnded without a stop: the runner finished or failed
    # Details withheld from the wire (CLI stderr, exception text) are in the log.
    see_log = f" (details in the sidecar log, turn {turn_id})"
    if isinstance(error, ApiError):
        return error.code.value, error.message + (see_log if error.detail else "")
    if error is None:
        return ErrorCode.SDK_ERROR.value, "runner ended without a result"
    if isinstance(error, asyncio.CancelledError):
        return ErrorCode.CANCELLED.value, "turn cancelled"
    return ErrorCode.INTERNAL.value, "internal error" + see_log


def _merge_system_prompt(
    *,
    base_path: Path | None,
    system_prompt: str | None,
    append_system_prompt: str | None,
) -> str | None:
    if system_prompt is not None:
        return system_prompt  # replaces CLAUDE.md entirely: don't even read it
    base = ""
    if base_path is not None and base_path.exists():
        base = base_path.read_text(encoding="utf-8")
    if append_system_prompt:
        return f"{base}\n\n{append_system_prompt}" if base else append_system_prompt
    return base or None


def _get_runner(settings: Settings) -> Runner:
    """The provider's run_turn with the operator's policy (from Settings) bound."""
    if settings.provider == "codex":
        codex_policy = codex_runner.CodexPolicy(
            sandbox=settings.codex_sandbox,
            env_passthrough=settings.codex_env_passthrough_names,
        )
        return functools.partial(codex_runner.run_turn, policy=codex_policy)
    claude_policy = claude_runner.ClaudePolicy(
        tools=settings.claude_tools_names,
        allowed_tools=settings.claude_allowed_tools_names,
        disallowed_tools=settings.claude_disallowed_tools_names,
        permission_mode=settings.claude_permission_mode,
        setting_sources=settings.claude_setting_sources_names,
        restricted=settings.claude_restricted,
        # Anything the operator routed to codex is none of the claude agent's business.
        withheld_env=settings.codex_env_passthrough_names,
        anthropic_api_key=(
            settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
        ),
    )
    return functools.partial(claude_runner.run_turn, policy=claude_policy)


def _to_sse(ev: RunnerEvent) -> dict[str, str]:
    if isinstance(ev, SessionEvent):
        return sse_event("session", {"sessionId": ev.session_id})
    if isinstance(ev, TextEvent):
        return sse_event("text", {"delta": ev.delta})
    if isinstance(ev, ToolUseEvent):
        return sse_event(
            "tool_use",
            {
                "name": ev.name,
                "args": ev.args,
                "toolUseId": ev.tool_use_id,
            },
        )
    if isinstance(ev, ToolResultEvent):
        return sse_event(
            "tool_result",
            {
                "name": ev.name,
                "ok": ev.ok,
                "toolUseId": ev.tool_use_id,
            },
        )
    if isinstance(ev, DoneEvent):
        return sse_event(
            "done",
            {
                "finalText": ev.final_text,
                "usage": {
                    "inputTokens": ev.input_tokens,
                    "outputTokens": ev.output_tokens,
                    "cacheReadInputTokens": ev.cache_read_input_tokens,
                    "cacheCreationInputTokens": ev.cache_creation_input_tokens,
                },
            },
        )
    raise TypeError(f"unknown runner event: {type(ev).__name__}")
