import asyncio
from contextlib import nullcontext
from pathlib import Path

import pytest

from sidecar.admission import Admission
from sidecar.events import SessionEvent
from sidecar.turn import Turn, TurnStopped


def _admission(app) -> Admission:
    # ASGITransport doesn't run the lifespan, so set up state manually.
    if not hasattr(app.state, "admission"):
        app.state.admission = Admission(4)
    return app.state.admission


async def _running_turn(admission: Admission, session_key: str) -> Turn:
    async def runner(_cwd):
        yield SessionEvent(session_id="s")
        await asyncio.Event().wait()

    turn = Turn(session_key=session_key, user_id=None, admission=admission, timeout_sec=30)
    turn.start(runner, lambda: nullcontext(Path("/tmp")))
    assert await turn.events.get() == SessionEvent(session_id="s")
    return turn


def test_cancel_unknown_session_returns_404(client):
    r = client.post(
        "/v1/sessions/never-active/cancel",
        headers={"Authorization": "Bearer test-secret"},
    )
    assert r.status_code == 404
    assert r.json() == {"code": "not_found", "message": "no active turn for sessionKey"}


def test_cancel_requires_bearer(client):
    r = client.post("/v1/sessions/anything/cancel")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_cancel_active_stops_the_turn_and_returns_202(app):
    from httpx import ASGITransport, AsyncClient

    turn = await _running_turn(_admission(app), "active-key")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post(
                "/v1/sessions/active-key/cancel",
                headers={"Authorization": "Bearer test-secret"},
            )
        assert r.status_code == 202
        assert r.json() == {"status": "accepted", "sessionKey": "active-key"}
        assert turn.events.get_nowait() == TurnStopped("cancelled")
    finally:
        turn.stop("disconnected")
        await asyncio.wait({turn.task})


@pytest.mark.asyncio
@pytest.mark.parametrize("path_key", ["team/task", "team%2Ftask"])
async def test_session_key_with_a_slash_can_be_cancelled(app, path_key):
    from httpx import ASGITransport, AsyncClient

    turn = await _running_turn(_admission(app), "team/task")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.post(
                f"/v1/sessions/{path_key}/cancel",
                headers={"Authorization": "Bearer test-secret"},
            )
        assert r.status_code == 202
        assert turn.events.get_nowait() == TurnStopped("cancelled")
    finally:
        turn.stop("disconnected")
        await asyncio.wait({turn.task})
