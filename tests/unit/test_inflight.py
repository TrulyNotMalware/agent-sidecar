import asyncio

import pytest

from sidecar.errors import ApiError, ErrorCode
from sidecar.inflight import InflightHandle, InflightRegistry


def _make_handle(session_key: str = "k") -> InflightHandle:
    async def noop():
        await asyncio.Event().wait()
    task = asyncio.create_task(noop())
    return InflightHandle(
        session_key=session_key,
        user_id=None,
        cancel_event=asyncio.Event(),
        task=task,
    )


@pytest.mark.asyncio
async def test_register_then_get():
    reg = InflightRegistry()
    h = _make_handle()
    await reg.register(h)
    assert await reg.get("k") is h
    h.task.cancel()


@pytest.mark.asyncio
async def test_get_unknown_returns_none():
    reg = InflightRegistry()
    assert await reg.get("nope") is None


@pytest.mark.asyncio
async def test_register_duplicate_raises_busy():
    reg = InflightRegistry()
    h1 = _make_handle()
    h2 = _make_handle()
    await reg.register(h1)
    with pytest.raises(ApiError) as exc:
        await reg.register(h2)
    assert exc.value.code == ErrorCode.BUSY
    h1.task.cancel()
    h2.task.cancel()


@pytest.mark.asyncio
async def test_unregister_removes_matching_handle():
    reg = InflightRegistry()
    h = _make_handle()
    await reg.register(h)
    await reg.unregister("k", h)
    assert await reg.get("k") is None
    h.task.cancel()


@pytest.mark.asyncio
async def test_unregister_with_stale_handle_is_noop():
    reg = InflightRegistry()
    h_live = _make_handle()
    h_stale = _make_handle()
    await reg.register(h_live)
    await reg.unregister("k", h_stale)
    assert await reg.get("k") is h_live
    h_live.task.cancel()
    h_stale.task.cancel()
