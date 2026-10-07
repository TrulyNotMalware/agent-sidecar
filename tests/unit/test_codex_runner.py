import asyncio
import json
import signal
from pathlib import Path
from typing import Any

import pytest

from sidecar import codex_runner
from sidecar.codex_runner import CodexPolicy, ensure_codex_auth, run_turn
from sidecar.errors import ApiError, ErrorCode
from sidecar.events import (
    DoneEvent,
    RunnerEvent,
    SessionEvent,
    TextEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnSpec,
)

BASE_CMD = ["codex", "exec", "--json", "--skip-git-repo-check", "--sandbox", "read-only"]


class _Pipe:
    """A readable pipe: returns the given chunks, then EOF — or blocks, like a quiet
    process, when `hang` is set (until the process is signalled)."""

    def __init__(
        self, chunks: list[bytes], *, hang: bool = False, proc: "FakeProc | None" = None
    ) -> None:
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
        wait_stuck: bool = False,
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
        # Python 3.12: returncode is set at exit, but wait() blocks until every pipe
        # is closed — a leftover process can keep stderr open.
        self._wait_stuck = wait_stuck
        if wait_stuck:
            self.returncode = returncode

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
        if self._wait_stuck:
            await asyncio.Event().wait()
        if self._hang and not self.signalled.is_set():
            await self.signalled.wait()
        for _ in range(5):  # let the stderr reader finish deterministically
            await asyncio.sleep(0)
        self.returncode = self._exit_code
        return self.returncode


def _install(monkeypatch: pytest.MonkeyPatch, proc: FakeProc) -> dict[str, Any]:
    calls: dict[str, Any] = {}

    async def fake_exec(*cmd: str, **kwargs: object) -> FakeProc:
        calls["cmd"] = list(cmd)
        calls["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    # Never signal a real process group from a unit test.
    monkeypatch.setattr(codex_runner, "_signal_group", lambda _pid, sig: proc.signal_group(sig))
    return calls


def _line(obj: dict[str, object]) -> bytes:
    return (json.dumps(obj) + "\n").encode()


COMPLETED = _line({"type": "turn.completed", "usage": {}})


async def _collect(
    spec: TurnSpec | None = None, *, policy: CodexPolicy | None = None
) -> list[RunnerEvent]:
    spec = TurnSpec(prompt="hi") if spec is None else spec
    return [ev async for ev in run_turn(spec, cwd=Path("/tmp"), policy=policy)]


async def test_maps_full_event_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    # The shapes codex 0.159 emits (recorded against a fake model API).
    tool_call = {
        "id": "item_1",
        "type": "mcp_tool_call",
        "server": "domain-tools",
        "tool": "echo",
        "arguments": {"text": "hi"},
        "result": None,
        "error": None,
        "status": "in_progress",
    }
    lines = [
        _line({"type": "thread.started", "thread_id": "t-1"}),
        _line({"type": "turn.started"}),
        _line({"type": "item.completed", "item": {"type": "agent_message", "text": "preamble"}}),
        _line({"type": "item.started", "item": tool_call}),
        _line({"type": "item.completed", "item": {**tool_call, "status": "completed"}}),
        _line({"type": "item.completed", "item": {"type": "agent_message", "text": "final"}}),
        _line(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 20,
                    "cached_input_tokens": 4,
                    "cache_write_input_tokens": 3,
                    "output_tokens": 6,
                    "reasoning_output_tokens": 0,
                },
            }
        ),
    ]
    calls = _install(monkeypatch, FakeProc(lines))

    events = await _collect()

    assert calls["cmd"] == [*BASE_CMD, "--", "-"]
    assert calls["kwargs"]["stdin"] == asyncio.subprocess.PIPE
    assert calls["kwargs"]["start_new_session"] is True
    assert events == [
        SessionEvent(session_id="t-1"),
        TextEvent(delta="preamble"),
        # Named like claude names the same tool.
        ToolUseEvent(name="mcp__domain-tools__echo", args={"text": "hi"}, tool_use_id="item_1"),
        ToolResultEvent(name="mcp__domain-tools__echo", ok=True, tool_use_id="item_1"),
        TextEvent(delta="final"),
        DoneEvent(
            final_text="final",  # the last message, like claude's result
            input_tokens=20,
            output_tokens=6,
            cache_read_input_tokens=4,
            cache_creation_input_tokens=3,
        ),
    ]


def test_mcp_tool_names_are_normalized_like_the_claude_cli() -> None:
    from sidecar.codex_runner import _mcp_name

    assert _mcp_name({"server": "v1.2 tools", "tool": "do.it"}) == "mcp__v1_2_tools__do_it"
    assert _mcp_name({"tool": "bare"}) == "bare"  # no server reported


async def test_prompt_goes_through_stdin_never_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = FakeProc([COMPLETED])
    calls = _install(monkeypatch, proc)
    big = "x" * 200_000  # past the old 100 KB argv guard

    await _collect(TurnSpec(prompt=big))

    assert big not in calls["cmd"]
    assert proc.stdin.data == big.encode()
    assert proc.stdin.closed  # EOF, or codex waits for "additional input"


async def test_system_prompt_is_the_new_sessions_developer_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tomllib

    proc = FakeProc([COMPLETED])
    calls = _install(monkeypatch, proc)

    await _collect(TurnSpec(prompt="hi", system_prompt='Be "brief".\nNo tables.'))

    cmd = calls["cmd"]
    override = cmd[cmd.index("-c") + 1]
    assert tomllib.loads(override) == {"developer_instructions": 'Be "brief".\nNo tables.'}
    assert cmd.index("-c") < cmd.index("--")
    assert proc.stdin.data == b"hi"  # the prompt alone


async def test_resume_passes_the_system_prompt_again(monkeypatch: pytest.MonkeyPatch) -> None:
    # codex keeps the thread's developer message on resume (no duplicate), but rebuilds
    # the history from the value passed in when it auto-compacts.
    proc = FakeProc([COMPLETED])
    calls = _install(monkeypatch, proc)

    await _collect(TurnSpec(prompt="hi", system_prompt="SYS", resume_session_id="sess-1"))

    cmd = calls["cmd"]
    assert cmd[cmd.index("-c") + 1] == 'developer_instructions="SYS"'
    assert cmd.index("-c") < cmd.index("resume")
    assert proc.stdin.data == b"hi"


async def test_a_too_long_system_prompt_is_not_repeated_on_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_runner, "_MAX_ARG_BYTES", 64)
    proc = FakeProc([COMPLETED])
    calls = _install(monkeypatch, proc)

    await _collect(TurnSpec(prompt="hi", system_prompt="S" * 100, resume_session_id="sess-1"))

    assert "-c" not in calls["cmd"]
    assert proc.stdin.data == b"hi"  # the first turn's copy is in the thread already


async def test_a_system_prompt_too_long_for_argv_goes_with_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_runner, "_MAX_ARG_BYTES", 64)
    proc = FakeProc([COMPLETED])
    calls = _install(monkeypatch, proc)

    await _collect(TurnSpec(prompt="hi", system_prompt="S" * 100))

    assert "-c" not in calls["cmd"]
    assert proc.stdin.data == b"S" * 100 + b"\n\nhi"


async def test_resume_session_id_extends_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install(monkeypatch, FakeProc([COMPLETED]))

    await _collect(TurnSpec(prompt="hi", resume_session_id="sess-9"))

    assert calls["cmd"] == [*BASE_CMD, "resume", "--", "sess-9", "-"]


async def test_flag_like_prompts_and_ids_are_not_parsed_as_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc([COMPLETED])
    calls = _install(monkeypatch, proc)

    await _collect(TurnSpec(prompt="--last", resume_session_id="-csandbox_mode=danger-full-access"))

    assert calls["cmd"][-4:] == ["resume", "--", "-csandbox_mode=danger-full-access", "-"]
    assert proc.stdin.data == b"--last"


async def test_ephemeral_for_stateless_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install(monkeypatch, FakeProc([COMPLETED]))

    await _collect(TurnSpec(prompt="hi", ephemeral=True))

    assert "--ephemeral" in calls["cmd"]
    assert calls["cmd"].index("--ephemeral") < calls["cmd"].index("--")


async def test_command_execution_maps_to_shell_events(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [
        _line(
            {
                "type": "item.started",
                "item": {"type": "command_execution", "id": "c-1", "command": "ls -la"},
            }
        ),
        _line(
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "id": "c-1", "exit_code": 1},
            }
        ),
        COMPLETED,
    ]
    _install(monkeypatch, FakeProc(lines))

    events = await _collect()

    assert events[0] == ToolUseEvent(name="shell", args={"command": "ls -la"}, tool_use_id="c-1")
    assert events[1] == ToolResultEvent(name="shell", ok=False, tool_use_id="c-1")


async def test_malformed_json_lines_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
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


async def test_event_lines_split_across_reads_are_reassembled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    whole = _line({"type": "item.completed", "item": {"type": "agent_message", "text": "Hi"}})
    _install(monkeypatch, FakeProc([whole[:10], whole[10:], COMPLETED]))

    events = await _collect()

    assert events[0] == TextEvent(delta="Hi")


async def test_oversized_event_line_is_skipped_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_runner, "_MAX_EVENT_LINE_BYTES", 64)
    huge = _line({"type": "item.completed", "item": {"type": "x", "output": "y" * 500}})
    _install(monkeypatch, FakeProc([huge[:100], huge[100:], COMPLETED]))

    events = await _collect()

    assert isinstance(events[-1], DoneEvent)


async def test_turn_failed_raises_sdk_error_and_reaps_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc([_line({"type": "turn.failed", "error": {"message": "quota exhausted"}})])
    _install(monkeypatch, proc)

    with pytest.raises(ApiError) as exc_info:
        await _collect()

    assert exc_info.value.code is ErrorCode.SDK_ERROR
    assert "quota exhausted" in exc_info.value.message
    assert proc.killed


async def test_transient_error_events_do_not_end_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    # codex reports "Reconnecting... n/5" as type=error while falling back to HTTPS.
    reconnect = _line({"type": "error", "message": "Reconnecting... 2/5"})
    _install(monkeypatch, FakeProc([reconnect, COMPLETED]))

    events = await _collect()

    assert isinstance(events[-1], DoneEvent)


async def test_error_event_is_reported_when_the_turn_never_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, FakeProc([_line({"type": "error", "message": "boom"})]))

    with pytest.raises(ApiError) as exc_info:
        await _collect()

    assert exc_info.value.code is ErrorCode.SDK_ERROR
    assert "boom" in exc_info.value.message


async def test_exit_zero_without_turn_completed_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # e.g. codex printed help text instead of running a turn
    _install(monkeypatch, FakeProc([b"Usage: codex exec ...\n"]))

    with pytest.raises(ApiError, match=r"without turn\.completed"):
        await _collect()


async def test_nonzero_exit_surfaces_the_end_of_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    noise = [f"log line {i}\n".encode() for i in range(300)]
    proc = FakeProc([], returncode=3, stderr_lines=[*noise, b"fatal: no auth\n"])
    _install(monkeypatch, proc)

    with pytest.raises(ApiError) as exc_info:
        await _collect()

    error = exc_info.value
    assert error.code is ErrorCode.SDK_ERROR
    assert error.message == "codex exited with code 3"  # stderr never goes on the wire
    assert error.detail is not None
    assert error.detail.endswith("fatal: no auth\n")  # the log gets the end, not the start


async def test_turn_failed_with_a_bare_string_error_keeps_the_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, FakeProc([_line({"type": "turn.failed", "error": "rate limited"})]))

    with pytest.raises(ApiError) as exc_info:
        await _collect()

    assert exc_info.value.message == "rate limited"


async def test_codex_that_cannot_be_started_is_an_sdk_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def missing(*_cmd: str, **_kwargs: object) -> FakeProc:
        raise FileNotFoundError(2, "No such file or directory", "codex")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)

    with pytest.raises(ApiError) as exc_info:
        await _collect()

    assert exc_info.value.code is ErrorCode.SDK_ERROR
    assert exc_info.value.message == "codex could not be started (FileNotFoundError)"


async def test_nonzero_exit_carries_what_codex_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [_line({"type": "error", "message": "stream disconnected: 503"})]
    _install(monkeypatch, FakeProc(lines, returncode=1, stderr_lines=[b"internal trace\n"]))

    with pytest.raises(ApiError) as exc_info:
        await _collect()

    assert exc_info.value.message == "codex exited with code 1: stream disconnected: 503"


async def test_provider_messages_are_scrubbed_of_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed: dict[str, object] = {
        "type": "turn.failed",
        "error": {"message": "401: bad key sk-proj-abcdef123456"},
    }
    _install(monkeypatch, FakeProc([_line(failed)]))

    with pytest.raises(ApiError) as exc_info:
        await _collect()

    assert "abcdef123456" not in exc_info.value.message
    assert exc_info.value.message == "401: bad key sk-<redacted>"


async def test_stderr_reader_failure_does_not_skip_the_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc([], hang=True)
    proc.stderr = _BrokenPipe()  # type: ignore[assignment]  # stands in for the pipe
    _install(monkeypatch, proc)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await _collect()

    assert proc.killed


async def test_mcp_override_adds_config_flags_and_token_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install(monkeypatch, FakeProc([COMPLETED]))

    events = await _collect(
        TurnSpec(
            prompt="hi",
            mcp_server_url="https://app.example/mcp",
            mcp_server_name="domain-tools",
            turn_token="tok-xyz",
        )
    )

    assert events[-1].__class__ is DoneEvent
    assert 'mcp_servers.domain-tools.url="https://app.example/mcp"' in calls["cmd"]
    assert 'mcp_servers.domain-tools.bearer_token_env_var="SIDECAR_MCP_TURN_TOKEN"' in calls["cmd"]
    assert 'mcp_servers.domain-tools.default_tools_approval_mode="approve"' in calls["cmd"]
    assert calls["kwargs"]["env"]["SIDECAR_MCP_TURN_TOKEN"] == "tok-xyz"


async def test_mcp_url_is_quoted_as_a_toml_string(monkeypatch: pytest.MonkeyPatch) -> None:
    import tomllib

    url = 'https://app.example/mcp?q="x"\\y'
    calls = _install(monkeypatch, FakeProc([COMPLETED]))

    await _collect(
        TurnSpec(prompt="hi", mcp_server_url=url, mcp_server_name="domain-tools", turn_token="t")
    )

    [override] = [a for a in calls["cmd"] if a.startswith("mcp_servers.domain-tools.url=")]
    assert tomllib.loads(override)["mcp_servers"]["domain-tools"]["url"] == url


async def test_without_mcp_override_no_config_flags_and_no_token_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIDECAR_MCP_TURN_TOKEN", "stale-from-parent")
    calls = _install(monkeypatch, FakeProc([COMPLETED]))

    await _collect()

    assert "-c" not in calls["cmd"]
    assert "SIDECAR_MCP_TURN_TOKEN" not in calls["kwargs"]["env"]


async def test_sandbox_is_always_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install(monkeypatch, FakeProc([COMPLETED]))

    await _collect(
        TurnSpec(prompt="hi", resume_session_id="sess-1"),
        policy=CodexPolicy(sandbox="workspace-write"),
    )

    cmd = calls["cmd"]
    # exec-level option: must come before the `resume` subcommand
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert cmd.index("--sandbox") < cmd.index("resume")


async def test_child_env_withholds_sidecar_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
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

    await _collect(policy=CodexPolicy(env_passthrough=("CUSTOM_PROVIDER_KEY",)))

    env = calls["kwargs"]["env"]
    for withheld in ("BEARER_SECRET", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "UNRELATED"):
        assert withheld not in env
    assert env["PATH"] == "/usr/bin"
    assert env["HTTPS_PROXY"] == "http://proxy:3128"
    assert env["LC_ALL"] == "C.UTF-8"
    assert env["CODEX_CA_CERTIFICATE"] == "/etc/ssl/corp.pem"
    assert env["OPENAI_BASE_URL"] == "https://gateway.internal/v1"
    assert env["CUSTOM_PROVIDER_KEY"] == "k"


async def test_cancelling_the_turn_terminates_the_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The caller (sidecar.turn.Turn) bounds a turn by cancelling the task that
    # iterates the runner; the runner must then stop codex and its children.
    proc = FakeProc([], hang=True)
    _install(monkeypatch, proc)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await _collect()

    # SIGTERM first; then the group is always swept, in case a member ignored it.
    assert proc.signals == [signal.SIGTERM, signal.SIGKILL]


async def test_group_is_killed_when_sigterm_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_runner, "_TERM_GRACE_SEC", 0.05)
    proc = FakeProc([], hang=True, ignore_sigterm=True)
    _install(monkeypatch, proc)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await _collect()

    assert proc.signals == [signal.SIGTERM, signal.SIGKILL]


async def test_closing_the_runner_early_terminates_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc([_line({"type": "thread.started", "thread_id": "t-1"})], hang=True)
    _install(monkeypatch, proc)

    agen = run_turn(TurnSpec(prompt="hi"), cwd=Path("/tmp"))
    assert await anext(agen) == SessionEvent(session_id="t-1")
    await agen.aclose()

    assert proc.killed


class _FakeLoginProc:
    def __init__(self, returncode: int, auth_path: Path | None = None) -> None:
        self.returncode = returncode
        self._auth_path = auth_path
        self.stdin_payload: bytes | None = None

    async def communicate(self, payload: bytes | None = None) -> tuple[bytes, bytes]:
        self.stdin_payload = payload
        if self.returncode == 0 and self._auth_path is not None:
            self._auth_path.write_text("{}")
        return b"", b""


async def test_ensure_codex_auth_noop_when_auth_file_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text("{}")

    async def unexpected_exec(*cmd: str, **kwargs: object) -> _FakeLoginProc:
        raise AssertionError("login must not be spawned when auth.json exists")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected_exec)

    assert await ensure_codex_auth(auth) is True


async def test_ensure_codex_auth_false_without_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert await ensure_codex_auth(tmp_path / "auth.json") is False


async def test_ensure_codex_auth_registers_key_via_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    auth = tmp_path / "auth.json"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    proc = _FakeLoginProc(returncode=0, auth_path=auth)
    calls: dict[str, Any] = {}

    async def fake_exec(*cmd: str, **kwargs: object) -> _FakeLoginProc:
        calls["cmd"] = list(cmd)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    assert await ensure_codex_auth(auth) is True
    assert calls["cmd"] == ["codex", "login", "--with-api-key"]
    assert proc.stdin_payload == b"sk-test"


async def test_ensure_codex_auth_false_when_login_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    proc = _FakeLoginProc(returncode=1)

    async def fake_exec(*cmd: str, **kwargs: object) -> _FakeLoginProc:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    assert await ensure_codex_auth(tmp_path / "auth.json") is False


async def test_ensure_codex_auth_false_when_codex_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    async def missing(*cmd: str, **kwargs: object) -> _FakeLoginProc:
        raise FileNotFoundError("codex")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)

    assert await ensure_codex_auth(tmp_path / "auth.json") is False


async def test_group_is_swept_after_codex_exits_on_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    # A shell command or MCP server codex started may outlive it.
    proc = FakeProc([COMPLETED])
    _install(monkeypatch, proc)

    await _collect()

    assert proc.signals and set(proc.signals) == {signal.SIGKILL}


async def test_finished_turn_does_not_hang_when_wait_is_stuck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Python 3.12: a leftover holding a pipe keeps wait() from returning after exit.
    monkeypatch.setattr(codex_runner, "_EXIT_WAIT_SEC", 0.05)
    _install(monkeypatch, FakeProc([COMPLETED], wait_stuck=True))

    async with asyncio.timeout(3):
        events = await _collect()

    assert isinstance(events[-1], DoneEvent)
