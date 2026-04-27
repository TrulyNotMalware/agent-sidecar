import asyncio

import pytest

from sidecar.concurrency import ConcurrencyGate
from sidecar.errors import ApiError, ErrorCode


@pytest.mark.asyncio
async def test_concurrency_cap_rejects_when_full():
    gate = ConcurrencyGate(max_concurrent=1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        async with gate.acquire():
            started.set()
            await release.wait()

    task = asyncio.create_task(hold())
    await started.wait()

    with pytest.raises(ApiError) as exc:
        async with gate.acquire():
            pass
    assert exc.value.code == ErrorCode.BUSY

    release.set()
    await task


@pytest.mark.asyncio
async def test_per_user_lock_blocks_same_user():
    gate = ConcurrencyGate(max_concurrent=10)
    started = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        async with gate.acquire(user_id="u1"):
            started.set()
            await release.wait()

    task = asyncio.create_task(hold())
    await started.wait()

    with pytest.raises(ApiError) as exc:
        async with gate.acquire(user_id="u1"):
            pass
    assert exc.value.code == ErrorCode.BUSY

    release.set()
    await task


@pytest.mark.asyncio
async def test_different_users_run_concurrently():
    gate = ConcurrencyGate(max_concurrent=10)

    async def hold(uid: str):
        async with gate.acquire(user_id=uid):
            await asyncio.sleep(0.01)

    await asyncio.gather(hold("a"), hold("b"))


@pytest.mark.asyncio
async def test_session_key_lock_blocks_same_session():
    gate = ConcurrencyGate(max_concurrent=10)
    started = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        async with gate.acquire(session_key="s1"):
            started.set()
            await release.wait()

    task = asyncio.create_task(hold())
    await started.wait()

    with pytest.raises(ApiError) as exc:
        async with gate.acquire(session_key="s1"):
            pass
    assert exc.value.code == ErrorCode.BUSY

    release.set()
    await task


@pytest.mark.asyncio
async def test_inflight_decrements_on_exit():
    gate = ConcurrencyGate(max_concurrent=2)
    async with gate.acquire(user_id="u1"):
        assert gate.inflight == 1
    assert gate.inflight == 0
