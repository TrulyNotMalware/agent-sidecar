import asyncio
import contextlib

import pytest

from sidecar.inflight import InflightHandle, InflightRegistry


def _make_handle(session_key: str, cancel_event: asyncio.Event) -> InflightHandle:
    async def keep_alive():
        with contextlib.suppress(asyncio.CancelledError):
            await cancel_event.wait()
    task = asyncio.create_task(keep_alive())
    return InflightHandle(
        session_key=session_key,
        user_id=None,
        cancel_event=cancel_event,
        task=task,
    )


@pytest.mark.asyncio
async def test_drain_with_no_inflight_returns_zero():
    reg = InflightRegistry()
    forced = await reg.drain(grace_sec=0.1)
    assert forced == 0


@pytest.mark.asyncio
async def test_drain_signals_cancel_event_and_handles_finish_naturally():
    reg = InflightRegistry()
    ev = asyncio.Event()
    h = _make_handle("k", ev)
    await reg.register(h)

    # Producer cooperatively unregisters when cancel_event fires.
    async def cooperative_consumer():
        await ev.wait()
        await reg.unregister("k", h)

    consumer = asyncio.create_task(cooperative_consumer())
    forced = await reg.drain(grace_sec=1.0)
    assert forced == 0  # finished within grace
    assert reg.active_count == 0
    consumer.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await consumer
    h.task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await h.task


@pytest.mark.asyncio
async def test_drain_force_cancels_after_grace():
    reg = InflightRegistry()
    ev = asyncio.Event()

    # Task that ignores cancel_event entirely — simulates a turn blocked deep
    # inside the SDK (e.g. waiting on subprocess output).
    async def stuck():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(60)

    task = asyncio.create_task(stuck())
    h = InflightHandle(session_key="stuck", user_id=None, cancel_event=ev, task=task)
    await reg.register(h)

    forced = await reg.drain(grace_sec=0.1)
    assert forced == 1
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.done()
    await reg.unregister("stuck", h)


@pytest.mark.asyncio
async def test_drain_waits_for_turn_tasks_not_polling_the_registry():
    # A turn unregisters itself only after its runner closed the CLI; drain must wait
    # on the task (cleanup), not just for the registry entry to disappear.
    reg = InflightRegistry()
    ev = asyncio.Event()
    cleaned = asyncio.Event()

    async def turn():
        await ev.wait()
        await asyncio.sleep(0.1)  # closing the CLI
        cleaned.set()

    task = asyncio.create_task(turn())
    h = InflightHandle(session_key="k", user_id=None, cancel_event=ev, task=task)
    await reg.register(h)

    forced = await reg.drain(grace_sec=2)

    assert forced == 0
    assert cleaned.is_set()
