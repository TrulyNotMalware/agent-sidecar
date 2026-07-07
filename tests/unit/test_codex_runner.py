import asyncio
import json
from pathlib import Path

import pytest

from sidecar.claude_runner import (
    DoneEvent,
    SessionEvent,
    TextEvent,
    ToolResultEvent,
    ToolUseEvent,
)
from sidecar.codex_runner import ensure_codex_auth, run_turn
from sidecar.errors import ApiError, ErrorCode


class _Lines:
    """Async line iterator mimicking asyncio StreamReader iteration."""

    def __init__(self, lines: list[bytes]) -> None:
        self._it = iter(lines)

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        await asyncio.sleep(0)  # yield to the loop like real pipe I/O does
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


class _HangingLines:
    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class FakeProc:
    def __init__(
        self,
        stdout_lines: list[bytes],
        *,
        returncode: int = 0,
        stderr_lines: list[bytes] | None = None,
        hang: bool = False,
    ) -> None:
        self.stdout = _HangingLines() if hang else _Lines(stdout_lines)
        self.stderr = _Lines(stderr_lines or [])
        self.returncode: int | None = None
        self._exit_code = returncode
        self.killed = False

    async def wait(self) -> int:
        # Yield a few times so the stderr drain task can finish deterministically.
        for _ in range(5):
            await asyncio.sleep(0)
        self.returncode = self._exit_code
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self._exit_code = -9


def _install(monkeypatch, proc: FakeProc) -> dict:
    calls: dict = {}

    async def fake_exec(*cmd, **kwargs):
        calls["cmd"] = list(cmd)
        calls["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return calls


def _line(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


async def _collect(
    *,
    prompt: str = "hi",
    system_prompt: str | None = None,
    resume_session_id: str | None = None,
    timeout_sec: float = 5,
) -> list:
    return [
        ev
        async for ev in run_turn(
            prompt=prompt,
            cwd=Path("/tmp"),
            system_prompt=system_prompt,
            resume_session_id=resume_session_id,
            mcp_config_path=None,
            timeout_sec=timeout_sec,
        )
    ]


async def test_maps_full_event_sequence(monkeypatch):
    lines = [
        _line({"type": "thread.started", "thread_id": "t-1"}),
        _line({
            "type": "item.started",
            "item": {
                "type": "mcp_tool_call",
                "id": "call-1",
                "tool": "lookup",
                "arguments": {"q": 1},
            },
        }),
        _line({
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "id": "call-1",
                "tool": "lookup",
                "status": "completed",
                "error": None,
            },
        }),
        _line({"type": "item.completed", "item": {"type": "agent_message", "text": "Hello"}}),
        _line({
            "type": "turn.completed",
            "usage": {"input_tokens": 10, "output_tokens": 5, "cached_input_tokens": 2},
        }),
    ]
    calls = _install(monkeypatch, FakeProc(lines))

    events = await _collect()

    assert calls["cmd"] == ["codex", "exec", "--json", "--skip-git-repo-check", "hi"]
    assert calls["kwargs"]["stdin"] == asyncio.subprocess.DEVNULL
    assert events == [
        SessionEvent(session_id="t-1"),
        ToolUseEvent(name="lookup", args={"q": 1}, tool_use_id="call-1"),
        ToolResultEvent(name="lookup", ok=True, tool_use_id="call-1"),
        TextEvent(delta="Hello"),
        DoneEvent(
            final_text="Hello",
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=2,
            cache_creation_input_tokens=None,
        ),
    ]


async def test_command_execution_maps_to_shell_events(monkeypatch):
    lines = [
        _line({
            "type": "item.started",
            "item": {"type": "command_execution", "id": "c-1", "command": "ls -la"},
        }),
        _line({
            "type": "item.completed",
            "item": {"type": "command_execution", "id": "c-1", "exit_code": 1},
        }),
        _line({"type": "turn.completed", "usage": {}}),
    ]
    _install(monkeypatch, FakeProc(lines))

    events = await _collect()

    assert events[0] == ToolUseEvent(name="shell", args={"command": "ls -la"}, tool_use_id="c-1")
    assert events[1] == ToolResultEvent(name="shell", ok=False, tool_use_id="c-1")


async def test_malformed_json_lines_are_skipped(monkeypatch):
    lines = [b"not json\n", b"\n", _line({"type": "turn.completed", "usage": {}})]
    _install(monkeypatch, FakeProc(lines))

    events = await _collect()

    assert events == [
        DoneEvent(
            final_text="",
            input_tokens=0,
            output_tokens=0,
            cache_read_input_tokens=None,
            cache_creation_input_tokens=None,
        )
    ]


async def test_turn_failed_raises_sdk_error_and_reaps_process(monkeypatch):
    proc = FakeProc([_line({"type": "turn.failed", "error": {"message": "quota exhausted"}})])
    _install(monkeypatch, proc)

    with pytest.raises(ApiError) as exc_info:
        await _collect()

    assert exc_info.value.code is ErrorCode.SDK_ERROR
    assert "quota exhausted" in exc_info.value.message
    assert proc.killed


async def test_error_event_raises_sdk_error(monkeypatch):
    _install(monkeypatch, FakeProc([_line({"type": "error", "message": "boom"})]))

    with pytest.raises(ApiError) as exc_info:
        await _collect()

    assert exc_info.value.code is ErrorCode.SDK_ERROR
    assert "boom" in exc_info.value.message


async def test_nonzero_exit_surfaces_stderr_tail(monkeypatch):
    proc = FakeProc([], returncode=3, stderr_lines=[b"fatal: no auth\n"])
    _install(monkeypatch, proc)

    with pytest.raises(ApiError) as exc_info:
        await _collect()

    assert exc_info.value.code is ErrorCode.SDK_ERROR
    assert "code 3" in exc_info.value.message
    assert "fatal: no auth" in exc_info.value.message


async def test_prompt_over_limit_rejected_before_spawn(monkeypatch):
    calls = _install(monkeypatch, FakeProc([]))

    with pytest.raises(ApiError) as exc_info:
        await _collect(prompt="x" * 100_001)

    assert exc_info.value.code is ErrorCode.BAD_REQUEST
    assert "cmd" not in calls


async def test_resume_session_id_extends_argv(monkeypatch):
    lines = [_line({"type": "turn.completed", "usage": {}})]
    calls = _install(monkeypatch, FakeProc(lines))

    await _collect(resume_session_id="sess-9")

    assert calls["cmd"] == [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "resume",
        "sess-9",
        "hi",
    ]


async def test_system_prompt_prepended_to_prompt(monkeypatch):
    lines = [_line({"type": "turn.completed", "usage": {}})]
    calls = _install(monkeypatch, FakeProc(lines))

    await _collect(system_prompt="SYS")

    assert calls["cmd"][-1] == "SYS\n\nhi"


async def test_timeout_raises_and_kills_process(monkeypatch):
    proc = FakeProc([], hang=True)
    _install(monkeypatch, proc)

    with pytest.raises(ApiError) as exc_info:
        await _collect(timeout_sec=0.05)

    assert exc_info.value.code is ErrorCode.TIMEOUT


class _FakeLoginProc:
    def __init__(self, returncode: int, auth_path: Path | None = None) -> None:
        self.returncode = returncode
        self._auth_path = auth_path
        self.stdin_payload: bytes | None = None

    async def communicate(self, payload: bytes | None = None):
        self.stdin_payload = payload
        if self.returncode == 0 and self._auth_path is not None:
            self._auth_path.write_text("{}")
        return b"", b""


async def test_ensure_codex_auth_noop_when_auth_file_exists(monkeypatch, tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text("{}")

    async def unexpected_exec(*cmd, **kwargs):
        raise AssertionError("login must not be spawned when auth.json exists")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected_exec)

    assert await ensure_codex_auth(auth) is True


async def test_ensure_codex_auth_false_without_key(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert await ensure_codex_auth(tmp_path / "auth.json") is False


async def test_ensure_codex_auth_registers_key_via_login(monkeypatch, tmp_path):
    auth = tmp_path / "auth.json"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    proc = _FakeLoginProc(returncode=0, auth_path=auth)
    calls: dict = {}

    async def fake_exec(*cmd, **kwargs):
        calls["cmd"] = list(cmd)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    assert await ensure_codex_auth(auth) is True
    assert calls["cmd"] == ["codex", "login", "--with-api-key"]
    assert proc.stdin_payload == b"sk-test"


async def test_ensure_codex_auth_false_when_login_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    proc = _FakeLoginProc(returncode=1)

    async def fake_exec(*cmd, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    assert await ensure_codex_auth(tmp_path / "auth.json") is False
