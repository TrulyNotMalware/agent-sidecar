import asyncio
from contextlib import nullcontext
from pathlib import Path

from sidecar.claude_runner import DoneEvent, SessionEvent
from sidecar.concurrency import ConcurrencyGate
from sidecar.errors import ApiError, ErrorCode
from sidecar.inflight import InflightHandle, InflightRegistry
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
        "gate_session_key": "k",
        "gate": ConcurrencyGate(4),
        "registry": InflightRegistry(),
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

    gate, registry = ConcurrencyGate(4), InflightRegistry()
    turn = _turn(gate=gate, registry=registry)
    turn.start(runner, _workspace)

    items = await _until_ended(turn)

    assert items == [SessionEvent(session_id="s"), DONE, TurnEnded(None, None)]
    assert registry.active_count == 0
    assert gate.inflight == 0


async def test_stop_is_signalled_at_once_but_released_only_after_cleanup():
    slow = SlowToClose()
    gate, registry = ConcurrencyGate(4), InflightRegistry()
    turn = _turn(gate=gate, registry=registry)
    turn.start(slow.run, _workspace)
    assert await _next(turn) == SessionEvent(session_id="s")

    turn.stop("cancelled")

    # The marker is there immediately; the reservation is still held during cleanup.
    assert turn.events.get_nowait() == TurnStopped("cancelled")
    assert await registry.get("k") is not None
    assert gate.inflight == 1

    turn.stop("timeout")  # a second stop must not interrupt the cleanup
    ended = await _next(turn)

    assert isinstance(ended, TurnEnded)
    assert ended.stop_reason == "cancelled"
    assert slow.cleanup_finished
    assert registry.active_count == 0
    assert gate.inflight == 0


async def test_timeout_stops_the_turn_even_if_nobody_reads_the_queue():
    slow = SlowToClose()
    turn = _turn(timeout_sec=0.05)
    turn.start(slow.run, _workspace)

    await asyncio.sleep(0.5)  # consumer stuck elsewhere (e.g. a slow client's send)

    items = await _until_ended(turn)
    assert items[:2] == [SessionEvent(session_id="s"), TurnStopped("timeout")]
    assert items[-1].stop_reason == "timeout"
    assert slow.cleanup_finished


async def test_cancel_event_stops_the_turn():
    slow = SlowToClose()
    turn = _turn()
    turn.start(slow.run, _workspace)
    await _next(turn)

    turn.cancel_event.set()

    assert await _next(turn) == TurnStopped("cancelled")
    await _until_ended(turn)


async def test_busy_session_key_ends_the_turn_without_opening_the_runner():
    registry = InflightRegistry()
    holder = asyncio.create_task(asyncio.Event().wait())
    await registry.register(
        InflightHandle(session_key="k", user_id=None, cancel_event=asyncio.Event(), task=holder)
    )
    opened = False

    def runner(_cwd):
        nonlocal opened
        opened = True
        raise AssertionError("must not be opened")

    turn = _turn(registry=registry)
    turn.start(runner, _workspace)
    [ended] = await _until_ended(turn)

    assert isinstance(ended.error, ApiError)
    assert ended.error.code is ErrorCode.BUSY
    assert not opened
    holder.cancel()


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
    registry = InflightRegistry()
    turn = _turn(registry=registry)
    turn.start(SlowToClose().run, _workspace)

    turn.stop("disconnected")  # same loop tick: _main has not started yet

    assert await _next(turn) == TurnStopped("disconnected")
    assert await _next(turn) == TurnEnded(stop_reason="disconnected", error=None)
    assert registry.active_count == 0
