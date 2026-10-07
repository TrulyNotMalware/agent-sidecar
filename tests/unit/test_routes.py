import dataclasses
import functools
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import NoReturn

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sidecar.config import Settings
from sidecar.events import Runner, RunnerEvent, TurnSpec
from sidecar.turn import Turn, TurnEnded, TurnStopped


def _settings(**overrides: object) -> Settings:
    # Raw env-style values (plain str for a SecretStr, unknown literals) are what these
    # tests feed through pydantic-settings' validation, so they are untyped on purpose.
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]  # raw values under test


def _install_recording_runner(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Swap the runner accessor for a fake async generator that records kwargs.

    The fake yields a single terminal DoneEvent so the SSE stream closes and the
    sync TestClient collects the full response.
    """
    from sidecar.events import DoneEvent
    from sidecar.routes import converse as converse_mod

    recorded: dict[str, object] = {}

    def fake_get_runner(settings: Settings) -> Runner:
        async def fake_run_turn(spec: TurnSpec, *, cwd: Path) -> AsyncGenerator[RunnerEvent, None]:
            recorded.update(dataclasses.asdict(spec), cwd=cwd)
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


def test_x_turn_token_header_reaches_runner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = _install_recording_runner(monkeypatch)

    r = client.post(
        "/v1/converse",
        json={"sessionKey": "turn-k", "prompt": "hi"},
        headers={"Authorization": "Bearer test-secret", "X-Turn-Token": "turn-abc"},
    )

    assert r.status_code == 200
    assert recorded["turn_token"] == "turn-abc"


def test_absent_turn_token_reaches_runner_as_none(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = _install_recording_runner(monkeypatch)

    r = client.post(
        "/v1/converse",
        json={"sessionKey": "turn-k2", "prompt": "hi"},
        headers={"Authorization": "Bearer test-secret"},
    )

    assert r.status_code == 200
    assert recorded["turn_token"] is None


def test_converse_requires_bearer(client: TestClient) -> None:
    r = client.post("/v1/converse", json={"sessionKey": "k", "prompt": "hi"})
    assert r.status_code == 401
    # Same {"code", "message"} shape as every other error (openapi `Error`).
    assert r.json() == {"code": "unauthorized", "message": "missing bearer token"}
    assert r.headers["www-authenticate"] == "Bearer"


def test_converse_rejects_wrong_bearer(client: TestClient) -> None:
    r = client.post(
        "/v1/converse",
        json={"sessionKey": "k", "prompt": "hi"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401


def test_cancel_unknown_session_returns_404(client: TestClient) -> None:
    r = client.post(
        "/v1/sessions/some-key/cancel",
        headers={"Authorization": "Bearer test-secret"},
    )
    assert r.status_code == 404


def test_invalid_body_returns_400_with_error_schema(client: TestClient) -> None:
    r = client.post(
        "/v1/converse",
        json={"sessionKey": "", "prompt": ""},  # both empty -> validation fail
        headers={"Authorization": "Bearer test-secret"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "bad_request"
    assert "sessionKey" in body["message"] or "prompt" in body["message"]


def test_unknown_field_returns_400(client: TestClient) -> None:
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


def _hold(app: FastAPI, session_key: str, user_id: str | None = None) -> Turn:
    """Reserve admission slots as if another turn were running (never started)."""
    admission = app.state.admission
    turn = Turn(session_key=session_key, user_id=user_id, admission=admission, timeout_sec=5)
    admission.reserve(turn)
    return turn


def test_converse_busy_same_session_key_returns_429(client: TestClient, app: FastAPI) -> None:
    held = _hold(app, "dup-key")
    try:
        r = client.post(
            "/v1/converse",
            json={"sessionKey": "dup-key", "prompt": "hi"},
            headers={"Authorization": "Bearer test-secret"},
        )
    finally:
        app.state.admission.release(held)

    assert r.status_code == 429
    assert r.json()["code"] == "busy"
    assert "dup-key" in r.json()["message"]
    assert len(r.headers["x-turn-id"]) == 32  # the converse.reject log line has it too


@pytest.mark.parametrize("mode", ["session", "stateless"])
def test_converse_busy_same_session_key_in_either_mode(
    client: TestClient, app: FastAPI, mode: str
) -> None:
    # /cancel addresses a turn by its sessionKey, so stateless turns hold it too.
    held = _hold(app, "dup-key", None)
    try:
        r = client.post(
            "/v1/converse",
            json={"sessionKey": "dup-key", "prompt": "hi", "mode": mode},
            headers={"Authorization": "Bearer test-secret"},
        )
    finally:
        app.state.admission.release(held)

    assert r.status_code == 429


def test_converse_busy_same_user_returns_429(client: TestClient, app: FastAPI) -> None:
    held = _hold(app, "held-key", "u1")
    try:
        r = client.post(
            "/v1/converse",
            json={"sessionKey": "other-key", "prompt": "hi"},
            headers={"Authorization": "Bearer test-secret", "X-User-Id": "u1"},
        )
    finally:
        app.state.admission.release(held)

    assert r.status_code == 429
    assert r.json()["code"] == "busy"
    assert "u1" in r.json()["message"]


def test_finished_turn_releases_its_reservation(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_recording_runner(monkeypatch)
    for _ in range(2):  # the second turn is only accepted if the first released
        r = client.post(
            "/v1/converse",
            json={"sessionKey": "again-k", "prompt": "hi"},
            headers={"Authorization": "Bearer test-secret", "X-User-Id": "again-u"},
        )
        assert r.status_code == 200
        assert "event: done" in r.text
    assert app.state.admission.inflight == 0


def test_a_failure_building_the_response_reserves_nothing(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reservation is the handler's last step, so nothing can leave a turn running
    # (and its sessionKey busy until the timeout) without a response to stop it.
    from sidecar.routes import converse as converse_mod

    _install_recording_runner(monkeypatch)

    def broken_response(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("response could not be built")

    monkeypatch.setattr(converse_mod, "EventSourceResponse", broken_response)
    with pytest.raises(RuntimeError):
        client.post(
            "/v1/converse",
            json={"sessionKey": "broken-k", "prompt": "hi"},
            headers={"Authorization": "Bearer test-secret"},
        )

    assert app.state.admission.inflight == 0


def test_flag_like_session_id_is_rejected_before_streaming(client: TestClient) -> None:
    r = client.post(
        "/v1/converse",
        json={"sessionKey": "k", "prompt": "hi", "sessionId": "--last"},
        headers={"Authorization": "Bearer test-secret"},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "bad_request"
    assert "sessionId" in r.json()["message"]


def test_bearer_scheme_is_case_insensitive(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_recording_runner(monkeypatch)

    r = client.post(
        "/v1/converse",
        json={"sessionKey": "scheme-k", "prompt": "hi"},
        headers={"Authorization": "bearer test-secret"},
    )

    assert r.status_code == 200


def test_empty_bearer_token_is_rejected(client: TestClient) -> None:
    r = client.post(
        "/v1/converse",
        json={"sessionKey": "k", "prompt": "hi"},
        headers={"Authorization": "Bearer "},
    )
    assert r.status_code == 401


def test_claude_runner_withholds_codex_passthrough_names() -> None:
    from sidecar.routes.converse import _get_runner

    runner = _get_runner(_settings(bearer_secret="x", codex_env_passthrough="AZURE_OPENAI_KEY"))

    assert isinstance(runner, functools.partial)
    assert runner.keywords["policy"].withheld_env == ("AZURE_OPENAI_KEY",)


def test_terminal_error_mapping() -> None:
    from sidecar.errors import ApiError, ErrorCode
    from sidecar.routes.converse import _terminal_error

    def mapped(item: TurnStopped | TurnEnded) -> tuple[str, str]:
        return _terminal_error(item, 90, "t1")

    assert mapped(TurnStopped("timeout")) == ("timeout", "turn exceeded 90s")
    assert mapped(TurnStopped("cancelled"))[0] == "cancelled"
    assert mapped(TurnStopped("shutdown"))[0] == "cancelled"
    assert mapped(TurnEnded(None, None)) == ("sdk_error", "runner ended without a result")
    # What the provider reported goes out as is ...
    reported = ApiError(ErrorCode.SDK_ERROR, "quota exhausted")
    assert mapped(TurnEnded(None, reported)) == ("sdk_error", "quota exhausted")
    # ... internals never do: the message points at the log instead.
    see_log = " (details in the sidecar log, turn t1)"
    withheld = ApiError(ErrorCode.SDK_ERROR, "codex exited with code 1", detail="stderr: x")
    assert mapped(TurnEnded(None, withheld)) == ("sdk_error", "codex exited with code 1" + see_log)
    assert mapped(TurnEnded(None, PermissionError("/var/lib/x"))) == (
        "internal",
        "internal error" + see_log,
    )


def test_stream_carries_the_turn_id(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_recording_runner(monkeypatch)

    r = client.post(
        "/v1/converse",
        json={"sessionKey": "turn-id-k", "prompt": "hi"},
        headers={"Authorization": "Bearer test-secret"},
    )

    assert r.status_code == 200
    assert len(r.headers["x-turn-id"]) == 32


def test_codex_runner_gets_sandbox_and_passthrough_bound() -> None:
    from sidecar.routes.converse import _get_runner

    runner = _get_runner(
        _settings(
            bearer_secret="x",
            provider="codex",
            codex_sandbox="workspace-write",
            codex_env_passthrough="AZURE_OPENAI_KEY",
        )
    )

    from sidecar.codex_runner import CodexPolicy

    assert isinstance(runner, functools.partial)
    assert runner.keywords == {
        "policy": CodexPolicy(sandbox="workspace-write", env_passthrough=("AZURE_OPENAI_KEY",))
    }


def test_claude_runner_gets_the_agent_policy_bound() -> None:
    from sidecar.routes.converse import _get_runner

    runner = _get_runner(
        _settings(
            bearer_secret="x",
            claude_tools="",
            claude_allowed_tools="WebFetch, Bash(git status:*)",
            claude_permission_mode="default",
            claude_setting_sources="project",
        )
    )

    assert isinstance(runner, functools.partial)
    policy = runner.keywords["policy"]
    assert policy.tools == ()
    assert policy.allowed_tools == ("WebFetch", "Bash(git status:*)")
    assert policy.permission_mode == "default"
    assert policy.setting_sources == ("project",)


def test_claude_policy_defaults() -> None:
    from sidecar.routes.converse import _get_runner

    runner = _get_runner(_settings(bearer_secret="x"))

    assert isinstance(runner, functools.partial)
    policy = runner.keywords["policy"]
    assert policy.tools is None  # CLI default toolset unless CLAUDE_TOOLS is set
    assert policy.permission_mode == "dontAsk"
    assert policy.setting_sources == ()


async def test_codex_resume_requires_an_id_issued_for_the_session_key(tmp_path: Path) -> None:
    from sidecar.models import ConverseRequest
    from sidecar.routes.converse import _preflight_resume
    from sidecar.session import remember_session_id

    sid = "01a0ec1c-14d2-7d12-aaa7-47100d58f161"
    codex = _settings(bearer_secret="x", provider="codex", workspace_root=tmp_path)
    claude = _settings(bearer_secret="x", workspace_root=tmp_path)
    body = ConverseRequest.model_validate({"sessionKey": "k", "prompt": "hi", "sessionId": sid})

    rejected = await _preflight_resume(body, codex)
    assert rejected is not None and rejected.status_code == 400
    assert await _preflight_resume(body, claude) is None  # claude scopes transcripts by cwd

    remember_session_id("k", sid, root=tmp_path)
    assert await _preflight_resume(body, codex) is None


async def test_an_unreadable_session_record_is_a_json_500(tmp_path: Path) -> None:
    import hashlib

    from sidecar.models import ConverseRequest
    from sidecar.routes.converse import _preflight_resume

    record = tmp_path / ".session-ids" / hashlib.sha256(b"k").hexdigest()
    record.mkdir(parents=True)  # reading it raises IsADirectoryError
    codex = _settings(bearer_secret="x", provider="codex", workspace_root=tmp_path)
    sid = "01a0ec1c-14d2-7d12-aaa7-47100d58f161"
    body = ConverseRequest.model_validate({"sessionKey": "k", "prompt": "hi", "sessionId": sid})

    rejected = await _preflight_resume(body, codex)

    assert rejected is not None
    assert rejected.status_code == 500


@pytest.mark.parametrize(("header", "limit"), [("X-User-Id", 256), ("X-Turn-Token", 4096)])
def test_header_length_limits_from_the_contract_are_enforced(
    client: TestClient, header: str, limit: int
) -> None:
    r = client.post(
        "/v1/converse",
        json={"sessionKey": "k", "prompt": "hi"},
        headers={"Authorization": "Bearer test-secret", header: "x" * (limit + 1)},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "bad_request"


def test_system_prompt_replaces_claude_md_without_reading_it(tmp_path: Path) -> None:
    from sidecar.routes.converse import _merge_system_prompt

    unreadable = tmp_path / "CLAUDE.md"
    unreadable.mkdir()  # reading it would raise IsADirectoryError

    assert (
        _merge_system_prompt(
            base_path=unreadable, system_prompt="only this", append_system_prompt="ignored"
        )
        == "only this"
    )


def test_append_system_prompt_follows_the_documented_join(tmp_path: Path) -> None:
    from sidecar.routes.converse import _merge_system_prompt

    base = tmp_path / "CLAUDE.md"
    base.write_text("BASE\n")

    assert (
        _merge_system_prompt(base_path=base, system_prompt=None, append_system_prompt="MORE")
        == "BASE\n\n\nMORE"
    )
    assert (
        _merge_system_prompt(base_path=None, system_prompt=None, append_system_prompt="MORE")
        == "MORE"
    )
    assert (
        _merge_system_prompt(base_path=base, system_prompt=None, append_system_prompt=None)
        == "BASE\n"
    )
    assert (
        _merge_system_prompt(base_path=None, system_prompt=None, append_system_prompt=None) is None
    )


def test_wrong_bearer_on_cancel_gets_the_same_401_shape(client: TestClient) -> None:
    r = client.post("/v1/sessions/k/cancel", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    assert r.json() == {"code": "unauthorized", "message": "invalid bearer token"}
    assert r.headers["www-authenticate"] == "Bearer"


def test_auth_is_checked_before_header_validation(client: TestClient) -> None:
    r = client.post(
        "/v1/converse",
        json={"sessionKey": "k", "prompt": "hi"},
        headers={"X-User-Id": "x" * 300},  # invalid, but unauthenticated first
    )
    assert r.status_code == 401


def test_stateless_turn_asks_the_runner_not_to_persist(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = _install_recording_runner(monkeypatch)

    r = client.post(
        "/v1/converse",
        json={"sessionKey": "k-stateless", "prompt": "hi", "mode": "stateless"},
        headers={"Authorization": "Bearer test-secret"},
    )

    assert r.status_code == 200
    assert recorded["ephemeral"] is True


def test_prompt_size_is_bounded(client: TestClient) -> None:
    r = client.post(
        "/v1/converse",
        json={"sessionKey": "k", "prompt": "x" * 1_000_001},
        headers={"Authorization": "Bearer test-secret"},
    )
    assert r.status_code == 400
