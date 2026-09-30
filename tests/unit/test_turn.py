import asyncio
from contextlib import nullcontext
from pathlib import Path

import pytest

from sidecar.admission import Admission
from sidecar.errors import ApiError, ErrorCode
from sidecar.events import DoneEvent, SessionEvent
from sidecar.turn import Turn, TurnEnded, TurnStopped

DONE = DoneEvent(
    final_text="ok",
    input_tokens=1,
    output_tokens=1,
    cache_read_input_tokens=None,
    cache_creation_input_tokens=None,
)


def _turn(**overrides) -> Turn:
    kwargs = {
        "session_key": "k",
        "user_id": "u",
        "admission": Admission(4),
        "timeout_sec": 5,
    }
    kwargs.update(overrides)
    return Turn(**kwargs)


def _workspace():
    return nullcontext(Path("/tmp"))


async def _next(turn: Turn):
    return await asyncio.wait_for(turn.events.get(), timeout=5)


async def _until_ended(turn: Turn) -> list:
    items = []
    while not items or not isinstance(items[-1], TurnEnded):
        items.append(await _next(turn))
    return items


class SlowToClose:
    """A runner whose cleanup takes a while, like the SDK closing the CLI."""

    def __init__(self) -> None:
        self.cleanup_finished = False

    async def run(self, _cwd):
        try:
            yield SessionEvent(session_id="s")
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.2)  # only completes if the task is cancelled once
            self.cleanup_finished = True


async def test_events_are_followed_by_exactly_one_turn_ended():
    async def runner(_cwd):
        yield SessionEvent(session_id="s")
        yield DONE

    admission = Admission(4)
    turn = _turn(admission=admission)
    turn.start(runner, _workspace)

    items = await _until_ended(turn)

    assert items == [SessionEvent(session_id="s"), DONE, TurnEnded(None, None)]
    assert admission.inflight == 0


async def test_stop_is_signalled_at_once_but_released_only_after_cleanup():
    slow = SlowToClose()
    admission = Admission(4)
    turn = _turn(admission=admission)
    turn.start(slow.run, _workspace)
    assert await _next(turn) == SessionEvent(session_id="s")

    turn.stop("cancelled")

    # The marker is there immediately; the reservation is still held during cleanup.
    assert turn.events.get_nowait() == TurnStopped("cancelled")
    assert admission.get("k") is turn
    assert admission.inflight == 1

    turn.stop("timeout")  # a second stop must not interrupt the cleanup
    ended = await _next(turn)

    assert isinstance(ended, TurnEnded)
    assert ended.stop_reason == "cancelled"
    assert slow.cleanup_finished
    assert admission.inflight == 0


async def test_timeout_stops_the_turn_even_if_nobody_reads_the_queue():
    slow = SlowToClose()
    turn = _turn(timeout_sec=0.05)
    turn.start(slow.run, _workspace)

    await asyncio.sleep(0.5)  # consumer stuck elsewhere (e.g. a slow client's send)

    items = await _until_ended(turn)
    assert items[:2] == [SessionEvent(session_id="s"), TurnStopped("timeout")]
    assert items[-1].stop_reason == "timeout"
    assert slow.cleanup_finished


async def test_start_reserves_before_the_task_runs():
    admission = Admission(4)
    turn = _turn(admission=admission)

    turn.start(SlowToClose().run, _workspace)

    # Synchronously, in the same loop tick: the route relies on this to answer 429.
    assert admission.get("k") is turn
    turn.stop("disconnected")
    await _until_ended(turn)


async def test_busy_start_raises_without_opening_the_runner():
    admission = Admission(4)
    admission.reserve(_turn(admission=admission))  # another turn on the same key
    opened = False

    def runner(_cwd):
        nonlocal opened
        opened = True
        raise AssertionError("must not be opened")

    turn = _turn(admission=admission)
    with pytest.raises(ApiError) as exc:
        turn.start(runner, _workspace)

    assert exc.value.code is ErrorCode.BUSY
    assert turn.task is None
    await asyncio.sleep(0)  # a task created anyway would have run by now
    assert not opened
    assert turn.events.empty()


async def test_runner_failure_is_carried_by_turn_ended():
    async def runner(_cwd):
        yield SessionEvent(session_id="s")
        raise ApiError(ErrorCode.SDK_ERROR, "boom")

    turn = _turn()
    turn.start(runner, _workspace)
    items = await _until_ended(turn)

    assert items[-1].stop_reason is None
    assert isinstance(items[-1].error, ApiError)
    assert items[-1].error.message == "boom"


async def test_stop_before_the_task_runs_still_ends_the_turn():
    admission = Admission(4)
    turn = _turn(admission=admission)
    turn.start(SlowToClose().run, _workspace)

    turn.stop("disconnected")  # same loop tick: _main has not started yet

    assert await _next(turn) == TurnStopped("disconnected")
    assert await _next(turn) == TurnEnded(stop_reason="disconnected", error=None)
    assert admission.inflight == 0
    await asyncio.wait({turn.task})
    assert turn.events.empty()  # _main never ran, so nothing follows TurnEnded


async def test_span_gets_the_status_but_not_the_exception_text(monkeypatch):
    # The default span recording would export the exception text and its cause chain
    # (CLI stderr, output lines) unscrubbed.
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import StatusCode

    from sidecar import turn as turn_module

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(turn_module, "tracer", provider.get_tracer("test"))

    async def runner(_cwd):
        yield SessionEvent(session_id="s")
        try:
            raise RuntimeError("stderr: private words sk-proj-abcdef123456")
        except RuntimeError as cause:
            raise ApiError(ErrorCode.SDK_ERROR, "claude CLI failed", detail="x") from cause

    turn = _turn()
    turn.start(runner, _workspace)
    await _until_ended(turn)

    [span] = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["outcome"] == "sdk_error"
    exported = repr([(e.name, dict(e.attributes)) for e in span.events])
    assert "claude CLI failed" in exported
    assert "private words" not in exported
