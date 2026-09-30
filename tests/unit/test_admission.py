import asyncio
from contextlib import nullcontext
from pathlib import Path

import pytest

from sidecar.admission import Admission
from sidecar.errors import ApiError, ErrorCode
from sidecar.events import SessionEvent
from sidecar.observability.metrics import INFLIGHT
from sidecar.turn import Turn, TurnEnded, TurnStopped


def _turn(admission: Admission, session_key: str = "k", user_id: str | None = None) -> Turn:
    return Turn(session_key=session_key, user_id=user_id, admission=admission, timeout_sec=5)


def _busy(admission: Admission, turn: Turn) -> str:
    with pytest.raises(ApiError) as exc:
        admission.reserve(turn)
    assert exc.value.code is ErrorCode.BUSY
    return exc.value.message


def test_one_turn_per_session_key_and_per_user():
    a = Admission(max_concurrent=10)
    held = _turn(a, "k", "u1")
    a.reserve(held)

    assert "'k'" in _busy(a, _turn(a, "k", None))
    assert "'u1'" in _busy(a, _turn(a, "other", "u1"))

    a.reserve(_turn(a, "k2", "u2"))  # different key and user: fine
    a.reserve(_turn(a, "k3", None))  # no X-User-Id: only the key counts
    assert a.get("k") is held
    assert a.inflight == 3


def test_global_cap():
    a = Admission(max_concurrent=1)
    a.reserve(_turn(a, "a"))

    assert "cap" in _busy(a, _turn(a, "b"))


def test_a_rejected_reservation_takes_nothing():
    a = Admission(max_concurrent=1)
    first = _turn(a, "a", "u1")
    a.reserve(first)
    _busy(a, _turn(a, "b", "u2"))  # over the cap: neither "b" nor "u2" may stick

    a.release(first)
    a.reserve(_turn(a, "b", "u2"))


def test_release_frees_every_slot_and_ignores_a_turn_it_does_not_hold():
    a = Admission(max_concurrent=4)
    held = _turn(a, "k", "u")
    a.reserve(held)

    a.release(_turn(a, "k", "u"))  # same key, but not the holder: no-op
    assert a.get("k") is held

    a.release(held)
    a.release(held)  # twice is harmless
    assert a.get("k") is None
    assert a.inflight == 0
    a.reserve(_turn(a, "k", "u"))


def test_inflight_gauge_follows_reservations():
    a = Admission(max_concurrent=4)
    t = _turn(a)
    a.reserve(t)
    assert INFLIGHT._value.get() == 1
    _busy(a, _turn(a))
    assert INFLIGHT._value.get() == 1
    a.release(t)
    a.release(t)
    assert INFLIGHT._value.get() == 0


async def test_drain_with_nothing_in_flight():
    assert await Admission(max_concurrent=4).drain(grace_sec=0.1) == 0


async def test_drain_stops_each_turn_once_and_waits_for_its_cleanup():
    a = Admission(max_concurrent=4)
    cleaned = asyncio.Event()

    async def runner(_cwd):
        try:
            yield SessionEvent(session_id="s")
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.1)  # closing the CLI; a second cancel would cut it short
            cleaned.set()

    turn = _turn(a)
    turn.start(runner, lambda: nullcontext(Path("/tmp")))
    assert await turn.events.get() == SessionEvent(session_id="s")

    forced = await a.drain(grace_sec=2)

    assert forced == 0
    assert cleaned.is_set()
    assert turn.events.get_nowait() == TurnStopped("shutdown")
    ended = turn.events.get_nowait()
    assert isinstance(ended, TurnEnded)
    assert ended.stop_reason == "shutdown"
    assert a.inflight == 0


async def test_drain_force_cancels_a_turn_whose_cleanup_hangs():
    a = Admission(max_concurrent=4)

    async def runner(_cwd):
        try:
            yield SessionEvent(session_id="s")
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(60)  # a CLI that never exits

    turn = _turn(a, "stuck")
    turn.start(runner, lambda: nullcontext(Path("/tmp")))
    await turn.events.get()

    forced = await a.drain(grace_sec=0.1)

    assert forced == 1
    await asyncio.wait({turn.task}, timeout=1)
    assert turn.task.done()
    assert a.inflight == 0  # released on the way out, even when forced
