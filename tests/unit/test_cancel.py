import asyncio
import contextlib

import pytest

from sidecar.inflight import InflightHandle


def test_cancel_unknown_session_returns_404(client):
    r = client.post(
        "/v1/sessions/never-active/cancel",
        headers={"Authorization": "Bearer test-secret"},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "not_found"


def test_cancel_requires_bearer(client):
    r = client.post("/v1/sessions/anything/cancel")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_cancel_active_sets_event_and_returns_202(app):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # ASGITransport doesn't run lifespan, so set up state manually.
        from sidecar.inflight import InflightRegistry
        if not hasattr(app.state, "inflight"):
            app.state.inflight = InflightRegistry()

        cancel_event = asyncio.Event()

        async def keep_alive():
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_event.wait()

        task = asyncio.create_task(keep_alive())
        handle = InflightHandle(
            session_key="active-key",
            user_id=None,
            cancel_event=cancel_event,
            task=task,
        )
        await app.state.inflight.register(handle)

        try:
            r = await ac.post(
                "/v1/sessions/active-key/cancel",
                headers={"Authorization": "Bearer test-secret"},
            )
            assert r.status_code == 202
            assert r.json() == {"status": "accepted", "sessionKey": "active-key"}

            # Yield once for the background escalation task to run cancel_event.set().
            for _ in range(20):
                if cancel_event.is_set():
                    break
                await asyncio.sleep(0.01)
            assert cancel_event.is_set()
        finally:
            cancel_event.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
