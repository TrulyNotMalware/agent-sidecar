"""One /v1/converse turn, run in its own asyncio task.

Why a separate task: the SSE response runs inside sse-starlette's anyio task group,
whose cancellation is level-triggered — once it fires (client disconnect, SIGTERM),
every later await in that task is cancelled again, so cleanup done there never
finishes (the SDK's close() died at its first checkpoint and the CLI outlived the
request). The turn task sits outside that group. It owns the concurrency
reservation, the workspace and the runner, and is cancelled at most once, so the
runner's cleanup (closing the CLI) always completes before the reservation is
released — a new turn on the same sessionKey cannot overlap the old CLI.

The SSE side only reads `Turn.events`. Runner events arrive in order, followed by
exactly one `TurnEnded`. When the turn is stopped (timeout, cancel, disconnect,
shutdown) a `TurnStopped` marker is queued immediately, so the client gets its
terminal frame without waiting for the CLI to exit.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .claude_runner import DoneEvent, RunnerEvent, ToolResultEvent, ToolUseEvent
from .concurrency import ConcurrencyGate
from .errors import ApiError
from .inflight import InflightHandle, InflightRegistry
from .observability.logging import get_logger
from .observability.metrics import INFLIGHT, TOKENS, TOOL_CALLS
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
        gate_session_key: str | None,
        gate: ConcurrencyGate,
        registry: InflightRegistry,
        timeout_sec: float,
        span_attributes: dict[str, str | bool] | None = None,
    ) -> None:
        self.session_key = session_key
        self.user_id = user_id
        self.events: asyncio.Queue[TurnItem] = asyncio.Queue()
        self.cancel_event = asyncio.Event()  # set by the cancel route and by drain()
        self._gate_session_key = gate_session_key
        self._gate = gate
        self._registry = registry
        self._timeout_sec = timeout_sec
        self._span_attributes = span_attributes or {}
        self._task: asyncio.Task | None = None
        self._started = False  # _main began executing (its finally will end the turn)
        self._stop_reason: StopReason | None = None

    def start(
        self,
        open_runner: Callable[[Path], AsyncIterator[RunnerEvent]],
        workspace: Callable[[], AbstractContextManager[Path]],
    ) -> None:
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
            # Cancelled before its first step: _main (and its finally) never runs, and
            # nothing was reserved yet. Keep the "always ends with TurnEnded" contract.
            self.events.put_nowait(TurnEnded(stop_reason=reason, error=None))

    async def _main(
        self,
        open_runner: Callable[[Path], AsyncIterator[RunnerEvent]],
        workspace: Callable[[], AbstractContextManager[Path]],
    ) -> None:
        self._started = True
        started = time.perf_counter()
        loop = asyncio.get_running_loop()
        deadline = loop.call_later(self._timeout_sec, self.stop, "timeout")
        cancel_watch = asyncio.create_task(self._stop_on_cancel_event())
        error: BaseException | None = None
        try:
            with tracer.start_as_current_span(
                "claude.turn", attributes=self._span_attributes
            ) as span:
                outcome = "completed"
                try:
                    await self._run(open_runner, workspace, span)
                except BaseException as exc:
                    outcome = exc.code.value if isinstance(exc, ApiError) else type(exc).__name__
                    raise
                finally:
                    span.set_attribute("outcome", self._stop_reason or outcome)
        except asyncio.CancelledError as exc:
            # Our own stop() (or drain's force-cancel). The runner has been closed on
            # the way out; the outcome is carried by TurnStopped / TurnEnded.
            error = exc
        except Exception as exc:  # noqa: BLE001 — reported to the client as TurnEnded
            error = exc
        finally:
            deadline.cancel()
            cancel_watch.cancel()
            self.events.put_nowait(TurnEnded(stop_reason=self._stop_reason, error=error))
            log.info(
                "turn.closed",
                session_key=self.session_key,
                stop_reason=self._stop_reason,
                error_type=type(error).__name__ if error else None,
                duration_seconds=round(time.perf_counter() - started, 3),
            )

    async def _run(
        self,
        open_runner: Callable[[Path], AsyncIterator[RunnerEvent]],
        workspace: Callable[[], AbstractContextManager[Path]],
        span,
    ) -> None:
        handle = InflightHandle(
            session_key=self.session_key,
            user_id=self.user_id,
            cancel_event=self.cancel_event,
            task=asyncio.current_task(),
        )
        await self._registry.register(handle)
        try:
            async with self._gate.acquire(
                user_id=self.user_id, session_key=self._gate_session_key
            ):
                INFLIGHT.inc()
                try:
                    with workspace() as cwd:
                        # aclosing: the runner (and the CLI it drives) is fully closed
                        # before the workspace, gate and registry entry are released.
                        async with contextlib.aclosing(open_runner(cwd)) as runner:
                            async for ev in runner:
                                _instrument(ev, span)
                                self.events.put_nowait(ev)
                finally:
                    INFLIGHT.dec()
        finally:
            await self._registry.unregister(self.session_key, handle)

    async def _stop_on_cancel_event(self) -> None:
        await self.cancel_event.wait()
        self.stop("cancelled")


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
        span.set_attribute("tokens.input", ev.input_tokens)
        span.set_attribute("tokens.output", ev.output_tokens)
