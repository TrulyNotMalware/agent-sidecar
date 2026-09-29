import asyncio
import json
import signal
from pathlib import Path

import pytest

from sidecar import codex_runner
from sidecar.claude_runner import (
    DoneEvent,
    SessionEvent,
    TextEvent,
    ToolResultEvent,
    ToolUseEvent,
)
from sidecar.codex_runner import ensure_codex_auth, run_turn
from sidecar.errors import ApiError, ErrorCode

BASE_CMD = ["codex", "exec", "--json", "--skip-git-repo-check", "--sandbox", "read-only"]


class _Pipe:
    """A readable pipe: returns the given chunks, then EOF — or blocks, like a quiet
    process, when `hang` is set (until the process is signalled)."""

    def __init__(self, chunks: list[bytes], *, hang: bool = False, proc=None) -> None:
        self._chunks = list(chunks)
        self._hang = hang
        self._proc = proc

    async def read(self, _n: int = -1) -> bytes:
        await asyncio.sleep(0)  # yield to the loop like real pipe I/O does
        if self._chunks:
            return self._chunks.pop(0)
        if self._hang and self._proc is not None:
            await self._proc.signalled.wait()
        return b""


class _BrokenPipe:
    async def read(self, _n: int = -1) -> bytes:
        raise ValueError("Separator is found, but chunk is longer than limit")


class _Stdin:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeProc:
    def __init__(
        self,
        stdout_lines: list[bytes],
        *,
        returncode: int = 0,
        stderr_lines: list[bytes] | None = None,
        hang: bool = False,
        ignore_sigterm: bool = False,
    ) -> None:
        self.pid = 424242
        self.stdin = _Stdin()
        self.stdout = _Pipe(stdout_lines, hang=hang, proc=self)
        self.stderr = _Pipe(stderr_lines or [])
        self.returncode: int | None = None
        self.signals: list[int] = []
        self.signalled = asyncio.Event()
        self._exit_code = returncode
        self._hang = hang
        self._ignore_sigterm = ignore_sigterm

    @property
    def killed(self) -> bool:
        return bool(self.signals)

    def signal_group(self, sig: int) -> None:
        self.signals.append(sig)
        if sig == signal.SIGTERM and self._ignore_sigterm:
            return
        self._exit_code = -sig
        self.signalled.set()

    async def wait(self) -> int:
        if self._hang and not self.signalled.is_set():
            await self.signalled.wait()
        for _ in range(5):  # let the stderr reader finish deterministically
            await asyncio.sleep(0)
        self.returncode = self._exit_code
        return self.returncode


def _install(monkeypatch, proc: FakeProc) -> dict:
    calls: dict = {}

    async def fake_exec(*cmd, **kwargs):
        calls["cmd"] = list(cmd)
        calls["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    # Never signal a real process group from a unit test.
    monkeypatch.setattr(codex_runner, "_signal_group", lambda _pid, sig: proc.signal_group(sig))
    return calls


def _line(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


COMPLETED = _line({"type": "turn.completed", "usage": {}})


async def _collect(
    *,
    prompt: str = "hi",
    system_prompt: str | None = None,
    resume_session_id: str | None = None,
    **kwargs,
) -> list:
    return [
        ev
        async for ev in run_turn(
            prompt=prompt,
            cwd=Path("/tmp"),
            system_prompt=system_prompt,
            resume_session_id=resume_session_id,
            mcp_config_path=None,
            **kwargs,
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

    assert calls["cmd"] == [*BASE_CMD, "--", "-"]
    assert calls["kwargs"]["stdin"] == asyncio.subprocess.PIPE
    assert calls["kwargs"]["start_new_session"] is True
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


async def test_prompt_goes_through_stdin_never_argv(monkeypatch):
    proc = FakeProc([COMPLETED])
    calls = _install(monkeypatch, proc)
    big = "x" * 200_000  # past the old 100 KB argv guard

    await _collect(prompt=big)

    assert big not in calls["cmd"]
    assert proc.stdin.data == big.encode()
    assert proc.stdin.closed  # EOF, or codex waits for "additional input"


async def test_system_prompt_prepended_to_prompt(monkeypatch):
    proc = FakeProc([COMPLETED])
    _install(monkeypatch, proc)

    await _collect(system_prompt="SYS")

    assert proc.stdin.data == b"SYS\n\nhi"


async def test_resume_session_id_extends_argv(monkeypatch):
    calls = _install(monkeypatch, FakeProc([COMPLETED]))

    await _collect(resume_session_id="sess-9")

    assert calls["cmd"] == [*BASE_CMD, "resume", "--", "sess-9", "-"]


async def test_flag_like_prompts_and_ids_are_not_parsed_as_flags(monkeypatch):
    proc = FakeProc([COMPLETED])
    calls = _install(monkeypatch, proc)

    await _collect(prompt="--last", resume_session_id="-csandbox_mode=danger-full-access")

    assert calls["cmd"][-4:] == ["resume", "--", "-csandbox_mode=danger-full-access", "-"]
    assert proc.stdin.data == b"--last"


async def test_ephemeral_for_stateless_turns(monkeypatch):
    calls = _install(monkeypatch, FakeProc([COMPLETED]))

    await _collect(ephemeral=True)

    assert "--ephemeral" in calls["cmd"]
    assert calls["cmd"].index("--ephemeral") < calls["cmd"].index("--")


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
        COMPLETED,
    ]
    _install(monkeypatch, FakeProc(lines))

    events = await _collect()

    assert events[0] == ToolUseEvent(name="shell", args={"command": "ls -la"}, tool_use_id="c-1")
    assert events[1] == ToolResultEvent(name="shell", ok=False, tool_use_id="c-1")


async def test_malformed_json_lines_are_skipped(monkeypatch):
    _install(monkeypatch, FakeProc([b"not json\n", b"\n", COMPLETED]))

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


async def test_event_lines_split_across_reads_are_reassembled(monkeypatch):
    whole = _line({"type": "item.completed", "item": {"type": "agent_message", "text": "Hi"}})
    _install(monkeypatch, FakeProc([whole[:10], whole[10:], COMPLETED]))

    events = await _collect()

    assert events[0] == TextEvent(delta="Hi")


async def test_oversized_event_line_is_skipped_not_fatal(monkeypatch):
    monkeypatch.setattr(codex_runner, "_MAX_EVENT_LINE_BYTES", 64)
    huge = _line({"type": "item.completed", "item": {"type": "x", "output": "y" * 500}})
    _install(monkeypatch, FakeProc([huge[:100], huge[100:], COMPLETED]))

    events = await _collect()

    assert isinstance(events[-1], DoneEvent)


async def test_turn_failed_raises_sdk_error_and_reaps_process(monkeypatch):
    proc = FakeProc([_line({"type": "turn.failed", "error": {"message": "quota exhausted"}})])
    _install(monkeypatch, proc)

    with pytest.raises(ApiError) as exc_info:
        await _collect()

    assert exc_info.value.code is ErrorCode.SDK_ERROR
    assert "quota exhausted" in exc_info.value.message
    assert proc.killed


async def test_transient_error_events_do_not_end_the_turn(monkeypatch):
    # codex reports "Reconnecting... n/5" as type=error while falling back to HTTPS.
    reconnect = _line({"type": "error", "message": "Reconnecting... 2/5"})
    _install(monkeypatch, FakeProc([reconnect, COMPLETED]))

    events = await _collect()

    assert isinstance(events[-1], DoneEvent)


async def test_error_event_is_reported_when_the_turn_never_completes(monkeypatch):
    _install(monkeypatch, FakeProc([_line({"type": "error", "message": "boom"})]))

    with pytest.raises(ApiError) as exc_info:
        await _collect()

    assert exc_info.value.code is ErrorCode.SDK_ERROR
    assert "boom" in exc_info.value.message


async def test_exit_zero_without_turn_completed_is_an_error(monkeypatch):
    # e.g. codex printed help text instead of running a turn
    _install(monkeypatch, FakeProc([b"Usage: codex exec ...\n"]))

    with pytest.raises(ApiError, match="without turn.completed"):
        await _collect()


async def test_nonzero_exit_surfaces_the_end_of_stderr(monkeypatch):
    noise = [f"log line {i}\n".encode() for i in range(300)]
    proc = FakeProc([], returncode=3, stderr_lines=[*noise, b"fatal: no auth\n"])
    _install(monkeypatch, proc)

    with pytest.raises(ApiError) as exc_info:
        await _collect()

    assert exc_info.value.code is ErrorCode.SDK_ERROR
    assert "code 3" in exc_info.value.message
    assert "fatal: no auth" in exc_info.value.message  # the last line, not the first


async def test_stderr_reader_failure_does_not_skip_the_kill(monkeypatch):
    proc = FakeProc([], hang=True)
    proc.stderr = _BrokenPipe()
    _install(monkeypatch, proc)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await _collect()

    assert proc.killed


async def test_mcp_override_adds_config_flags_and_token_env(monkeypatch):
    calls = _install(monkeypatch, FakeProc([COMPLETED]))

    events = await _collect(
        mcp_server_url="https://app.example/mcp",
        mcp_server_name="codecompanion",
        turn_token="tok-xyz",
    )

    assert events[-1].__class__ is DoneEvent
    assert 'mcp_servers.codecompanion.url="https://app.example/mcp"' in calls["cmd"]
    assert (
        'mcp_servers.codecompanion.bearer_token_env_var="CODECOMPANION_MCP_TOKEN"'
        in calls["cmd"]
    )
    assert 'mcp_servers.codecompanion.default_tools_approval_mode="approve"' in calls["cmd"]
    assert calls["kwargs"]["env"]["CODECOMPANION_MCP_TOKEN"] == "tok-xyz"


async def test_without_mcp_override_no_config_flags_and_no_token_env(monkeypatch):
    monkeypatch.setenv("CODECOMPANION_MCP_TOKEN", "stale-from-parent")
    calls = _install(monkeypatch, FakeProc([COMPLETED]))

    await _collect()

    assert "-c" not in calls["cmd"]
    assert "CODECOMPANION_MCP_TOKEN" not in calls["kwargs"]["env"]


async def test_sandbox_is_always_explicit(monkeypatch):
    calls = _install(monkeypatch, FakeProc([COMPLETED]))

    await _collect(resume_session_id="sess-1", sandbox="workspace-write")

    cmd = calls["cmd"]
    # exec-level option: must come before the `resume` subcommand
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert cmd.index("--sandbox") < cmd.index("resume")


async def test_child_env_withholds_sidecar_secrets(monkeypatch):
    for name, value in {
        "BEARER_SECRET": "sidecar-secret",
        "OPENAI_API_KEY": "sk-openai",
        "ANTHROPIC_API_KEY": "sk-ant",
        "PATH": "/usr/bin",
        "HTTPS_PROXY": "http://proxy:3128",
        "LC_ALL": "C.UTF-8",
        "CODEX_CA_CERTIFICATE": "/etc/ssl/corp.pem",
        "OPENAI_BASE_URL": "https://gateway.internal/v1",
        "CUSTOM_PROVIDER_KEY": "k",
        "UNRELATED": "x",
    }.items():
        monkeypatch.setenv(name, value)
    calls = _install(monkeypatch, FakeProc([COMPLETED]))

    await _collect(env_passthrough=("CUSTOM_PROVIDER_KEY",))

    env = calls["kwargs"]["env"]
    for withheld in ("BEARER_SECRET", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "UNRELATED"):
        assert withheld not in env
    assert env["PATH"] == "/usr/bin"
    assert env["HTTPS_PROXY"] == "http://proxy:3128"
    assert env["LC_ALL"] == "C.UTF-8"
    assert env["CODEX_CA_CERTIFICATE"] == "/etc/ssl/corp.pem"
    assert env["OPENAI_BASE_URL"] == "https://gateway.internal/v1"
    assert env["CUSTOM_PROVIDER_KEY"] == "k"


async def test_cancelling_the_turn_terminates_the_process_group(monkeypatch):
    # The caller (sidecar.turn.Turn) bounds a turn by cancelling the task that
    # iterates the runner; the runner must then stop codex and its children.
    proc = FakeProc([], hang=True)
    _install(monkeypatch, proc)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await _collect()

    assert proc.signals == [signal.SIGTERM]


async def test_group_is_killed_when_sigterm_is_ignored(monkeypatch):
    monkeypatch.setattr(codex_runner, "_TERM_GRACE_SEC", 0.05)
    proc = FakeProc([], hang=True, ignore_sigterm=True)
    _install(monkeypatch, proc)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await _collect()

    assert proc.signals == [signal.SIGTERM, signal.SIGKILL]


async def test_closing_the_runner_early_terminates_the_process(monkeypatch):
    proc = FakeProc([_line({"type": "thread.started", "thread_id": "t-1"})], hang=True)
    _install(monkeypatch, proc)

    agen = run_turn(
        prompt="hi", cwd=Path("/tmp"), system_prompt=None, resume_session_id=None,
        mcp_config_path=None,
    )
    assert await anext(agen) == SessionEvent(session_id="t-1")
    await agen.aclose()

    assert proc.killed


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


async def test_ensure_codex_auth_false_when_codex_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    async def missing(*cmd, **kwargs):
        raise FileNotFoundError("codex")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)

    assert await ensure_codex_auth(tmp_path / "auth.json") is False


async def test_group_is_swept_after_codex_exits_on_its_own(monkeypatch):
    # A shell command or MCP server codex started may outlive it.
    proc = FakeProc([COMPLETED])
    _install(monkeypatch, proc)

    await _collect()

    assert proc.signals == [signal.SIGKILL]
