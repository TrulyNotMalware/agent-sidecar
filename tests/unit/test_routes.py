def _install_recording_runner(monkeypatch):
    """Swap the runner accessor for a fake async generator that records kwargs.

    The fake yields a single terminal DoneEvent so the SSE stream closes and the
    sync TestClient collects the full response.
    """
    from sidecar.claude_runner import DoneEvent
    from sidecar.routes import converse as converse_mod

    recorded: dict = {}

    def fake_get_runner(settings):
        async def fake_run_turn(**kwargs):
            recorded.update(kwargs)
            yield DoneEvent(
                final_text="ok",
                input_tokens=0,
                output_tokens=0,
                cache_read_input_tokens=None,
                cache_creation_input_tokens=None,
            )

        return fake_run_turn

    monkeypatch.setattr(converse_mod, "_get_runner", fake_get_runner)
    return recorded


def test_x_turn_token_header_reaches_runner(client, monkeypatch):
    recorded = _install_recording_runner(monkeypatch)

    r = client.post(
        "/v1/converse",
        json={"sessionKey": "turn-k", "prompt": "hi"},
        headers={"Authorization": "Bearer test-secret", "X-Turn-Token": "turn-abc"},
    )

    assert r.status_code == 200
    assert recorded["turn_token"] == "turn-abc"


def test_absent_turn_token_reaches_runner_as_none(client, monkeypatch):
    recorded = _install_recording_runner(monkeypatch)

    r = client.post(
        "/v1/converse",
        json={"sessionKey": "turn-k2", "prompt": "hi"},
        headers={"Authorization": "Bearer test-secret"},
    )

    assert r.status_code == 200
    assert recorded["turn_token"] is None


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


def test_flag_like_session_id_is_rejected_before_streaming(client):
    r = client.post(
        "/v1/converse",
        json={"sessionKey": "k", "prompt": "hi", "sessionId": "--last"},
        headers={"Authorization": "Bearer test-secret"},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "bad_request"
    assert "sessionId" in r.json()["message"]


def test_bearer_scheme_is_case_insensitive(client, monkeypatch):
    _install_recording_runner(monkeypatch)

    r = client.post(
        "/v1/converse",
        json={"sessionKey": "scheme-k", "prompt": "hi"},
        headers={"Authorization": "bearer test-secret"},
    )

    assert r.status_code == 200


def test_empty_bearer_token_is_rejected(client):
    r = client.post(
        "/v1/converse",
        json={"sessionKey": "k", "prompt": "hi"},
        headers={"Authorization": "Bearer "},
    )
    assert r.status_code == 401


def test_claude_runner_withholds_codex_passthrough_names():
    from sidecar.config import Settings
    from sidecar.routes.converse import _get_runner

    runner = _get_runner(
        Settings(_env_file=None, bearer_secret="x", codex_env_passthrough="AZURE_OPENAI_KEY")
    )

    assert runner.keywords["withheld_env"] == ("AZURE_OPENAI_KEY",)


def test_terminal_error_mapping():
    from sidecar.routes.converse import _terminal_error
    from sidecar.turn import TurnEnded, TurnStopped

    assert _terminal_error(TurnStopped("timeout"), 90) == ("timeout", "turn exceeded 90s")
    assert _terminal_error(TurnStopped("cancelled"), 90)[0] == "cancelled"
    assert _terminal_error(TurnStopped("shutdown"), 90)[0] == "cancelled"
    assert _terminal_error(TurnEnded(None, None), 90) == (
        "sdk_error",
        "runner ended without a result",
    )
    assert _terminal_error(TurnEnded(None, ValueError("x")), 90) == ("internal", "ValueError: x")


def test_codex_runner_gets_sandbox_and_passthrough_bound():
    from sidecar.config import Settings
    from sidecar.routes.converse import _get_runner

    runner = _get_runner(
        Settings(
            _env_file=None,
            bearer_secret="x",
            provider="codex",
            codex_sandbox="workspace-write",
            codex_env_passthrough="AZURE_OPENAI_KEY",
        )
    )

    assert runner.keywords == {
        "sandbox": "workspace-write",
        "env_passthrough": ("AZURE_OPENAI_KEY",),
    }


def test_claude_runner_gets_the_agent_policy_bound():
    from sidecar.config import Settings
    from sidecar.routes.converse import _get_runner

    runner = _get_runner(
        Settings(
            _env_file=None,
            bearer_secret="x",
            claude_tools="",
            claude_allowed_tools="WebFetch, Bash(git status:*)",
            claude_permission_mode="default",
            claude_setting_sources="project",
        )
    )

    assert runner.keywords["tools"] == []
    assert runner.keywords["allowed_tools"] == ("WebFetch", "Bash(git status:*)")
    assert runner.keywords["permission_mode"] == "default"
    assert runner.keywords["setting_sources"] == ("project",)


def test_claude_policy_defaults():
    from sidecar.config import Settings
    from sidecar.routes.converse import _get_runner

    runner = _get_runner(Settings(_env_file=None, bearer_secret="x"))

    assert runner.keywords["tools"] is None  # CLI default toolset unless CLAUDE_TOOLS is set
    assert runner.keywords["permission_mode"] == "dontAsk"
    assert runner.keywords["setting_sources"] == ()
