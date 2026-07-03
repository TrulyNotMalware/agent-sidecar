def test_converse_requires_bearer(client):
    r = client.post("/v1/converse", json={"sessionKey": "k", "prompt": "hi"})
    assert r.status_code == 401


def test_converse_rejects_wrong_bearer(client):
    r = client.post(
        "/v1/converse",
        json={"sessionKey": "k", "prompt": "hi"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401


def test_cancel_unknown_session_returns_404(client):
    r = client.post(
        "/v1/sessions/some-key/cancel",
        headers={"Authorization": "Bearer test-secret"},
    )
    assert r.status_code == 404


def test_invalid_body_returns_400_with_error_schema(client):
    r = client.post(
        "/v1/converse",
        json={"sessionKey": "", "prompt": ""},  # both empty -> validation fail
        headers={"Authorization": "Bearer test-secret"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "bad_request"
    assert "sessionKey" in body["message"] or "prompt" in body["message"]


def test_unknown_field_returns_400(client):
    r = client.post(
        "/v1/converse",
        json={"sessionKey": "k", "prompt": "hi", "unknownField": 1},
        headers={"Authorization": "Bearer test-secret"},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "bad_request"


# The sync TestClient runs each ASGI request to completion, so an in-flight
# turn cannot be held open by a concurrent HTTP request. Instead, seed the
# in-flight state on the live app.state objects and hit the route.


def test_converse_busy_same_session_key_returns_429(client, app):
    import asyncio

    from sidecar.inflight import InflightHandle

    registry = app.state.inflight

    async def _register():
        handle = InflightHandle(
            session_key="dup-key",
            user_id=None,
            cancel_event=asyncio.Event(),
            task=asyncio.create_task(asyncio.sleep(0)),
        )
        await registry.register(handle)
        return handle

    handle = asyncio.run(_register())
    try:
        r = client.post(
            "/v1/converse",
            json={"sessionKey": "dup-key", "prompt": "hi"},
            headers={"Authorization": "Bearer test-secret"},
        )
    finally:
        asyncio.run(registry.unregister("dup-key", handle))

    assert r.status_code == 429
    assert r.json()["code"] == "busy"
    assert "dup-key" in r.json()["message"]


def test_converse_busy_same_user_returns_429(client, app):
    gate = app.state.gate
    # Simulate another turn holding the per-user slot.
    gate._user_inflight.add("u1")
    try:
        r = client.post(
            "/v1/converse",
            json={"sessionKey": "other-key", "prompt": "hi"},
            headers={"Authorization": "Bearer test-secret", "X-User-Id": "u1"},
        )
    finally:
        gate._user_inflight.discard("u1")

    assert r.status_code == 429
    assert r.json()["code"] == "busy"
    assert "u1" in r.json()["message"]
