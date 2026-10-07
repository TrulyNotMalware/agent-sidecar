"""The real CLIs — claude: the SDK's bundled binary; codex: `codex` on PATH — against
fake model APIs and a fake MCP server. No credentials, no quota.

What the fake-CLI tests cannot show: that the real CLIs accept the flags and config
the runners pass, what their event streams actually look like, and that the per-turn
MCP server is reached with the turn token.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol, TypedDict

import pytest

from .harness import HERE, SidecarServer, StartSidecar, converse, precondition, wait_until

pytestmark = pytest.mark.integration

TURN_TOKEN = "turn-token-for-the-mcp-server"
SYSTEM_MARK = "SYSTEM-PROMPT-MARKER"


class FakeServers(TypedDict):
    """What one call of the fakes fixture started."""

    api: str  # base URL of the fake model API
    mcp: str  # URL of the fake MCP server
    record: Path  # one NN.json per main-loop request the model API received
    auth_log: Path  # the Authorization header of every MCP request


class StartFakes(Protocol):
    """The fakes fixture: start the fake `kind` model API and the fake MCP server."""

    def __call__(self, kind: str, **env: str) -> FakeServers: ...


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def _serve(
    args: list[str], port: int, log: Path, env: dict[str, str] | None = None
) -> subprocess.Popen[bytes]:
    with log.open("wb") as out:
        proc = subprocess.Popen(
            [sys.executable, *args],
            stdout=out,
            stderr=subprocess.STDOUT,
            env={**os.environ, **(env or {})},
        )

    def _up() -> bool:
        try:
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                f"http://127.0.0.1:{port}/", timeout=0.5
            )
        except urllib.error.HTTPError:
            return True  # any HTTP answer means it is listening
        except OSError:
            return proc.poll() is not None
        return True

    if not wait_until(_up, timeout=15) or proc.poll() is not None:
        pytest.fail(f"harness: {args[0]} did not start:\n{log.read_text()}")
    return proc


@pytest.fixture
def fakes(tmp_path: Path) -> Iterator[StartFakes]:
    procs: list[subprocess.Popen[bytes]] = []

    def start(kind: str, **env: str) -> FakeServers:
        api_port, mcp_port = _free_port(), _free_port()
        record = tmp_path / f"{kind}-requests"
        auth_log = tmp_path / "mcp-auth.log"
        auth_log.touch()
        procs.append(
            _serve(
                [str(HERE / "fake_model_apis.py"), kind, str(api_port), str(record)],
                api_port,
                tmp_path / f"{kind}-api.log",
                env,
            )
        )
        procs.append(
            _serve(
                [str(HERE / "fake_mcp_server.py"), str(mcp_port), str(auth_log)],
                mcp_port,
                tmp_path / "mcp.log",
            )
        )
        return {
            "api": f"http://127.0.0.1:{api_port}",
            "mcp": f"http://127.0.0.1:{mcp_port}/mcp",
            "record": record,
            "auth_log": auth_log,
        }

    yield start
    for p in procs:
        p.kill()
        p.wait()


def _requests(record: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(record.glob("*.json"))]


def _claude(
    start_sidecar: StartSidecar, fakes: StartFakes, **env: str
) -> tuple[SidecarServer, FakeServers]:
    f = fakes("anthropic", **env)
    srv = start_sidecar(
        real_cli=True,
        ANTHROPIC_BASE_URL=f["api"],
        ANTHROPIC_API_KEY="sk-ant-fake-integration-key",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        MCP_SERVER_URL=f["mcp"],
    )
    return srv, f


def test_claude_turn_reaches_the_per_turn_mcp_server(
    start_sidecar: StartSidecar, fakes: StartFakes
) -> None:
    srv, f = _claude(start_sidecar, fakes)

    r = converse(
        srv.port,
        "real-claude",
        headers={"X-Turn-Token": TURN_TOKEN},
        body={"systemPrompt": SYSTEM_MARK},
        read_timeout=60,
    )

    assert r.event_names == ["session", "text", "tool_use", "tool_result", "text", "done"]
    tool_use, tool_result, done = r.events[2][1], r.events[3][1], r.events[-1][1]
    assert tool_use["name"] == "mcp__domain-tools__echo"
    assert tool_result == {
        "name": "mcp__domain-tools__echo",
        "ok": True,
        "toolUseId": tool_use["toolUseId"],
    }
    assert done["finalText"] == "final answer"  # the last message only, not the preamble
    assert done["usage"]["cacheReadInputTokens"] == 14  # two model calls x 7
    assert f"Bearer {TURN_TOKEN}" in f["auth_log"].read_text()
    [first, *_] = _requests(f["record"])
    assert SYSTEM_MARK in json.dumps(first["system"])


def test_claude_api_error_is_the_terminal_frame_scrubbed(
    start_sidecar: StartSidecar, fakes: StartFakes
) -> None:
    srv, _f = _claude(start_sidecar, fakes, FAKE_API_ERROR="400")

    r = converse(srv.port, "real-claude-err", read_timeout=60)

    assert r.event_names == ["session", "error"]  # the error prose is not a `text` event
    error = r.events[-1][1]
    assert error["code"] == "sdk_error"
    assert "model not available" in error["message"]
    assert "leakedkey" not in error["message"]


requires_codex = pytest.mark.skipif(shutil.which("codex") is None, reason="codex not on PATH")


def _codex(
    start_sidecar: StartSidecar, fakes: StartFakes, tmp_path: Path, config: str = "", **env: str
) -> tuple[SidecarServer, FakeServers]:
    f = fakes("openai", **env)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'model_provider = "fake"\n'
        # Hermetic: otherwise every `codex exec` also contacts chatgpt.com, ab.chatgpt.com
        # and github.com (updates, analytics, plugins/apps/skills, remote model list).
        "check_for_update_on_startup = false\n"
        f"{config}"
        # A provider without WebSockets: the stock one retries a WebSocket 5 times first.
        "[model_providers.fake]\n"
        'name = "fake"\n'
        f'base_url = "{f["api"]}/v1"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = true\n"
        "supports_websockets = false\n"
        "[analytics]\nenabled = false\n"
        "[feedback]\nenabled = false\n"
        "[features]\nplugins = false\napps = false\nremote_models = false\nskills = false\n"
    )
    srv = start_sidecar(
        provider="codex",
        real_cli=True,
        CODEX_HOME=str(codex_home),
        OPENAI_API_KEY="sk-fake-integration-key",  # startup runs `codex login --with-api-key`
        MCP_SERVER_URL=f["mcp"],
    )
    precondition((codex_home / "auth.json").exists(), "startup materialized auth.json")
    return srv, f


@requires_codex
def test_codex_turn_reaches_the_per_turn_mcp_server(
    start_sidecar: StartSidecar, fakes: StartFakes, tmp_path: Path
) -> None:
    srv, f = _codex(start_sidecar, fakes, tmp_path, FAKE_OPENAI_CALL_ECHO="1")

    r = converse(
        srv.port,
        "real-codex",
        prompt="the prompt",
        headers={"X-Turn-Token": TURN_TOKEN},
        body={"systemPrompt": SYSTEM_MARK},
    )

    assert r.event_names == ["session", "text", "tool_use", "tool_result", "text", "done"]
    tool_use, done = r.events[2][1], r.events[-1][1]
    assert tool_use["name"] == "mcp__domain-tools__echo"  # same naming as claude
    assert tool_use["args"] == {"text": "hi"}
    assert r.events[3][1]["ok"] is True
    assert done["finalText"] == "final answer"
    assert f"Bearer {TURN_TOKEN}" in f["auth_log"].read_text()
    first = _requests(f["record"])[0]
    messages = [(i.get("role"), json.dumps(i.get("content"))) for i in first["input"]]
    # The system prompt is a developer message, and the user message is the prompt alone.
    assert any(role == "developer" and SYSTEM_MARK in c for role, c in messages)
    assert not any(role == "user" and SYSTEM_MARK in c for role, c in messages)


@requires_codex
def test_codex_resume_does_not_repeat_the_system_prompt(
    start_sidecar: StartSidecar, fakes: StartFakes, tmp_path: Path
) -> None:
    srv, f = _codex(start_sidecar, fakes, tmp_path)

    first = converse(srv.port, "real-codex-resume", body={"systemPrompt": SYSTEM_MARK})
    session_id = first.events[0][1]["sessionId"]
    second = converse(
        srv.port, "real-codex-resume", body={"systemPrompt": SYSTEM_MARK, "sessionId": session_id}
    )

    assert second.event_names[-1] == "done"
    last = _requests(f["record"])[-1]
    assert json.dumps(last["input"]).count(SYSTEM_MARK) == 1


@requires_codex
def test_codex_system_prompt_survives_compaction_on_a_resumed_turn(
    start_sidecar: StartSidecar, fakes: StartFakes, tmp_path: Path
) -> None:
    # On auto-compaction codex rebuilds the thread from the developer instructions it
    # was started with *this* time — so a resume must pass them again.
    srv, f = _codex(
        start_sidecar,
        fakes,
        tmp_path,
        config="model_auto_compact_token_limit = 5000\nmodel_context_window = 1000000\n",
        FAKE_OPENAI_COMPACT_ON="COMPACT-NOW",
    )

    first = converse(srv.port, "real-codex-compact", body={"systemPrompt": SYSTEM_MARK})
    session_id = first.events[0][1]["sessionId"]
    second = converse(
        srv.port,
        "real-codex-compact",
        prompt="COMPACT-NOW",
        body={"systemPrompt": SYSTEM_MARK, "sessionId": session_id},
    )

    assert second.event_names[-1] == "done"
    requests = _requests(f["record"])
    precondition(any("COMPACTION" in json.dumps(r["input"]) for r in requests), "codex compacted")
    after = requests[-1]["input"]
    assert any(i.get("role") == "developer" and SYSTEM_MARK in json.dumps(i) for i in after)
    assert json.dumps(after).count(SYSTEM_MARK) == 1


@requires_codex
def test_codex_api_error_is_the_terminal_frame_scrubbed(
    start_sidecar: StartSidecar, fakes: StartFakes, tmp_path: Path
) -> None:
    srv, _f = _codex(start_sidecar, fakes, tmp_path, FAKE_API_ERROR="400")

    r = converse(srv.port, "real-codex-err")

    assert r.terminal_events == ["error"]
    error = r.events[-1][1]
    assert error["code"] == "sdk_error"
    assert "model not available" in error["message"]
    assert "leakedkey" not in error["message"]
