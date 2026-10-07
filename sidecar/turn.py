"""One /v1/converse turn, run in its own asyncio task.

Why a separate task: the SSE response runs inside sse-starlette's anyio task group,
whose cancellation is level-triggered — once it fires (client disconnect, SIGTERM),
every later await in that task is cancelled again, so cleanup done there never
finishes (the SDK's close() died at its first checkpoint and the CLI outlived the
request). The turn task sits outside that group. It owns the admission
reservation (taken by `start`), the workspace and the runner, and is cancelled at
most once, so the runner's cleanup (closing the CLI) always completes before the
reservation is released — a new turn on the same sessionKey cannot overlap the old CLI.

The SSE side only reads `Turn.events`. Runner events arrive in order, followed by
exactly one `TurnEnded`. When the turn is stopped (timeout, cancel, disconnect,
shutdown) a `TurnStopped` marker is queued immediately, so the client gets its
terminal frame without waiting for the CLI to exit.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from opentelemetry.trace import Status, StatusCode

from .admission import Admission
from .errors import ApiError
from .events import DoneEvent, RunnerEvent, ToolResultEvent, ToolUseEvent
from .observability.logging import get_logger
from .observability.metrics import TOKENS, TOOL_CALLS
from .observability.tracing import get_tracer

log = get_logger("sidecar.turn")
tracer = get_tracer("sidecar.turn")

StopReason = Literal["timeout", "cancelled", "disconnected", "shutdown"]


@dataclass(frozen=True)
class TurnStopped:
    """Queued the moment the turn is stopped; the runner is still being closed."""

    reason: StopReason


@dataclass(frozen=True)
class TurnEnded:
    """Last item on the queue: the runner is closed and the reservation released."""

    stop_reason: StopReason | None
    error: BaseException | None


TurnItem = RunnerEvent | TurnStopped | TurnEnded


class Turn:
    def __init__(
        self,
        *,
        session_key: str,
        user_id: str | None,
        admission: Admission,
        timeout_sec: float,
        span_attributes: dict[str, str | bool] | None = None,
        turn_id: str | None = None,
    ) -> None:
        # Correlates the client's error frame (and X-Turn-Id) with the sidecar's log.
        self.turn_id = turn_id or uuid.uuid4().hex
        self.session_key = session_key
        self.user_id = user_id
        self.events: asyncio.Queue[TurnItem] = asyncio.Queue()
        self._admission = admission
        self._timeout_sec = timeout_sec
        self._span_attributes = span_attributes or {}
        self._task: asyncio.Task | None = None
        self._started = False  # _main began executing (its finally will end the turn)
        self._stop_reason: StopReason | None = None

    @property
    def task(self) -> asyncio.Task | None:
        return self._task

    def start(
        self,
        open_runner: Callable[[Path], AsyncGenerator[RunnerEvent, None]],
        workspace: Callable[[], AbstractContextManager[Path]],
    ) -> None:
        """Reserve the turn's admission slots and start its task.

        Raises ApiError(BUSY) — before anything runs — if a limit is hit.
        """
        self._admission.reserve(self)
        self._task = asyncio.create_task(self._main(open_runner, workspace))

    def stop(self, reason: StopReason) -> None:
        """Stop the turn. Only the first call acts: a second cancel would interrupt
        the runner's cleanup and orphan the CLI."""
        if self._stop_reason is not None or self._task is None or self._task.done():
            return
        self._stop_reason = reason
        self.events.put_nowait(TurnStopped(reason))
        self._task.cancel()
        if not self._started:
            # Cancelled before its first step: _main (and its finally) never runs and
            # nothing was opened. Release here and keep the "always ends with
            # TurnEnded" contract.
            self._admission.release(self)
            self.events.put_nowait(TurnEnded(stop_reason=reason, error=None))

    async def _main(
        self,
        open_runner: Callable[[Path], AsyncGenerator[RunnerEvent, None]],
        workspace: Callable[[], AbstractContextManager[Path]],
    ) -> None:
        self._started = True
        started = time.perf_counter()
        loop = asyncio.get_running_loop()
        deadline = loop.call_later(self._timeout_sec, self.stop, "timeout")
        error: BaseException | None = None
        try:
            with tracer.start_as_current_span(
                "claude.turn",
                attributes=self._span_attributes,
                # The defaults would export the exception's text and its __cause__ chain
                # (CLI stderr, output lines): unscrubbed, and past LOG_PROMPTS.
                record_exception=False,
                set_status_on_exception=False,
            ) as span:
                outcome = "completed"
                try:
                    await self._run(open_runner, workspace, span)
                except BaseException as exc:
                    outcome = exc.code.value if isinstance(exc, ApiError) else type(exc).__name__
                    if not isinstance(exc, asyncio.CancelledError):
                        span.set_status(Status(StatusCode.ERROR, outcome))
                        span.add_event(
                            "exception",
                            {
                                "exception.type": type(exc).__name__,
                                # Only what the client was told; details are in the log.
                                "exception.message": exc.message
                                if isinstance(exc, ApiError)
                                else "",
                            },
                        )
                    raise
                finally:
                    span.set_attribute("outcome", self._stop_reason or outcome)
        except asyncio.CancelledError as exc:
            # Our own stop() (or drain's force-cancel). The runner has been closed on
            # the way out; the outcome is carried by TurnStopped / TurnEnded.
            error = exc
        except Exception as exc:  # noqa: BLE001 — reported to the client as TurnEnded
            error = exc
            if not isinstance(exc, ApiError):  # a bug or an environment problem
                log.error("turn.internal_error", error_type=type(exc).__name__, exc_info=exc)
        finally:
            deadline.cancel()
            # Released before TurnEnded is queued: once the stream has read it (and
            # ended), a new turn on this sessionKey is accepted.
            self._admission.release(self)
            self.events.put_nowait(TurnEnded(stop_reason=self._stop_reason, error=error))
            log.info(
                "turn.closed",
                session_key=self.session_key,
                stop_reason=self._stop_reason,
                error_type=type(error).__name__ if error else None,
                error_code=error.code.value if isinstance(error, ApiError) else None,
                error_message=error.message if isinstance(error, ApiError) else None,
                # CLI stderr / exception text: never sent to the client (secret-scrubbed).
                error_detail=error.detail if isinstance(error, ApiError) else None,
                duration_seconds=round(time.perf_counter() - started, 3),
            )

    async def _run(
        self,
        open_runner: Callable[[Path], AsyncGenerator[RunnerEvent, None]],
        workspace: Callable[[], AbstractContextManager[Path]],
        span,
    ) -> None:
        with workspace() as cwd:
            # aclosing: the runner (and the CLI it drives) is fully closed before the
            # workspace and the admission reservation are released.
            async with contextlib.aclosing(open_runner(cwd)) as runner:
                async for ev in runner:
                    _instrument(ev, span)
                    self.events.put_nowait(ev)


def _instrument(ev: RunnerEvent, span) -> None:
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
        TOKENS.labels(kind="cache_read").inc(ev.cache_read_input_tokens or 0)
        TOKENS.labels(kind="cache_creation").inc(ev.cache_creation_input_tokens or 0)
        span.set_attribute("tokens.input", ev.input_tokens)
        span.set_attribute("tokens.output", ev.output_tokens)
