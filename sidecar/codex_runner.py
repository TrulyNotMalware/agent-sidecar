import asyncio
import contextlib
import json
import os
import signal
from collections.abc import AsyncIterator, Iterable
from pathlib import Path

from .errors import ApiError, ErrorCode, provider_message
from .events import (
    DoneEvent,
    RunnerEvent,
    SessionEvent,
    TextEvent,
    ToolResultEvent,
    ToolUseEvent,
)
from .observability.logging import get_logger

log = get_logger("sidecar.codex")


def codex_auth_file(configured: Path | None = None) -> Path:
    """Where codex keeps auth.json: CODEX_AUTH_PATH if set, else $CODEX_HOME (~/.codex).

    `codex login` writes, and `codex exec` reads, $CODEX_HOME/auth.json; the sidecar's
    readiness check and startup materialization must look at the same file.
    """
    if configured is not None:
        return configured
    codex_home = os.environ.get("CODEX_HOME")
    return (Path(codex_home) if codex_home else Path.home() / ".codex") / "auth.json"


async def ensure_codex_auth(auth_path: Path | None = None) -> bool:
    """Materialize the codex auth state from OPENAI_API_KEY.

    codex-cli does not send OPENAI_API_KEY from the environment at request
    time; the key must be registered once via `codex login --with-api-key`,
    which writes ~/.codex/auth.json. No-op when the auth file already exists
    (subscription mode) or no key is present. Returns True when an auth file
    is available afterwards.
    """
    path = codex_auth_file(auth_path)
    if path.exists():
        return True
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            "codex",
            "login",
            "--with-api-key",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        return False  # /readyz reports the missing binary
    try:
        await asyncio.wait_for(proc.communicate(key.encode()), _LOGIN_TIMEOUT_SEC)
    except TimeoutError:
        await _terminate(proc)
        return False
    return proc.returncode == 0 and path.exists()


_MCP_TOKEN_ENV_VAR = "SIDECAR_MCP_TURN_TOKEN"
_LOGIN_TIMEOUT_SEC = 30

# codex and the shell commands the model runs inherit only these. The sidecar's own
# secrets (BEARER_SECRET, provider API keys) stay out: auth comes from CODEX_HOME.
_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "LANGUAGE", "TZ",
    "TMPDIR", "TMP", "TEMP", "CODEX_HOME", "RUST_LOG",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
    # CA bundles: codex's own first, then what tools run by the model look at.
    "CODEX_CA_CERTIFICATE", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE", "GIT_SSL_CAINFO", "PIP_CERT", "NODE_EXTRA_CA_CERTS",
    "NODE_OPTIONS",  # the npm `codex` entry point is a node wrapper (e.g. --use-openssl-ca)
    # Provider routing (not secrets): gateway URL and org/project attribution headers.
    "OPENAI_BASE_URL", "OPENAI_ORGANIZATION", "OPENAI_PROJECT",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
})
_ENV_ALLOWLIST_PREFIXES = ("LC_",)


def _child_env(passthrough: Iterable[str], extra: dict[str, str]) -> dict[str, str]:
    allowed = _ENV_ALLOWLIST | set(passthrough)
    env = {
        k: v
        for k, v in os.environ.items()
        if k in allowed or k.startswith(_ENV_ALLOWLIST_PREFIXES)
    }
    env.update(extra)
    return env


async def run_turn(
    *,
    prompt: str,
    cwd: Path,
    system_prompt: str | None,
    resume_session_id: str | None,
    mcp_config_path: Path | None,  # interface parity only; static MCP servers are claude-only
    mcp_server_url: str | None = None,
    mcp_server_name: str = "domain-tools",
    turn_token: str | None = None,
    sandbox: str = "read-only",
    env_passthrough: Iterable[str] = (),
    ephemeral: bool = False,
) -> AsyncIterator[RunnerEvent]:
    """Drive one `codex exec` turn and yield internal events.

    No timeout here: the caller bounds the turn (sidecar.turn.Turn) and cancels the
    task iterating this generator once; the process group is then terminated.
    """
    effective_prompt = f"{system_prompt}\n\n{prompt}".strip() if system_prompt else prompt

    # Session workspaces are plain scratch dirs; without the flag `codex exec`
    # refuses to run outside a trusted git repository. --sandbox is always explicit
    # so a config.toml cannot change the sandbox mode.
    cmd = ["codex", "exec", "--json", "--skip-git-repo-check", "--sandbox", sandbox]
    if ephemeral:
        cmd.append("--ephemeral")  # stateless: no rollout that could be resumed later

    # Per-turn MCP scoping: inject a streamable-HTTP server via dotted `-c` TOML
    # overrides and hand codex the bearer through an env var (never on argv).
    # Tool calls must be pre-approved ("approve"; "auto"/"prompt" cancel in headless
    # exec mode) — authorization is enforced server-side per call via the turn token.
    # Known limitation (codex 0.142.3): shell_environment_policy.exclude does not hide
    # the env var from model-run shell commands, so the model can read its own turn
    # token. Acceptable: the token is short-TTL and the model already holds the same
    # tool-call authority the token grants.
    mcp_scoped = mcp_server_url is not None and turn_token is not None
    if mcp_scoped:
        # The name is validated as a bare TOML key ([A-Za-z0-9_-]+); values are quoted.
        server = f"mcp_servers.{mcp_server_name}"
        cmd += [
            "-c", f"{server}.url={_toml_string(mcp_server_url)}",
            "-c", f"{server}.bearer_token_env_var={_toml_string(_MCP_TOKEN_ENV_VAR)}",
            "-c", f"{server}.default_tools_approval_mode={_toml_string('approve')}",
        ]

    # The prompt goes through stdin ("-"): no ARG_MAX limit and never visible in `ps`.
    # "--" ends option parsing, so a resume id can never be read as a flag either.
    if resume_session_id:
        cmd += ["resume", "--", resume_session_id, "-"]
    else:
        cmd += ["--", "-"]

    turn_env = _child_env(
        env_passthrough, {_MCP_TOKEN_ENV_VAR: turn_token} if mcp_scoped else {}
    )

    async def _stream() -> AsyncIterator[RunnerEvent]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=turn_env,
                # Own process group: shell commands and MCP servers codex starts (and the
                # native binary behind the npm node wrapper) are terminated with it.
                start_new_session=True,
            )
        except OSError as exc:  # not installed, not executable, …
            raise ApiError(
                ErrorCode.SDK_ERROR,
                f"codex could not be started ({type(exc).__name__})",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        stderr_tail = _Tail(_STDERR_TAIL_BYTES)
        stderr_task = asyncio.create_task(_drain(proc.stderr, stderr_tail))
        # Written concurrently with reading stdout: if codex ever produced a pipe's worth
        # of output before reading stdin, a sequential write would deadlock both sides.
        stdin_task = asyncio.create_task(_send_prompt(proc, effective_prompt))
        final_text_parts: list[str] = []
        completed = False
        last_error: str | None = None

        try:
            async for line in _ndjson_lines(proc.stdout):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ev_type = event.get("type")

                if ev_type == "thread.started":
                    thread_id = event.get("thread_id")
                    if thread_id:
                        yield SessionEvent(session_id=thread_id)

                elif ev_type == "item.started":
                    item = event.get("item", {})
                    for tool_ev in _tool_use_from_item(item):
                        yield tool_ev

                elif ev_type == "item.completed":
                    item = event.get("item", {})
                    item_type = item.get("type")

                    if item_type == "agent_message":
                        text = item.get("text") or ""
                        if text:
                            final_text_parts.append(text)
                            yield TextEvent(delta=text)
                    else:
                        result = _tool_result_from_item(item)
                        if result is not None:
                            yield result

                elif ev_type == "turn.completed":
                    completed = True
                    usage = event.get("usage") or {}
                    yield DoneEvent(
                        final_text="".join(final_text_parts),
                        input_tokens=int(usage.get("input_tokens") or 0),
                        output_tokens=int(usage.get("output_tokens") or 0),
                        cache_read_input_tokens=usage.get("cached_input_tokens"),
                        cache_creation_input_tokens=None,
                    )

                elif ev_type == "turn.failed":
                    raise ApiError(ErrorCode.SDK_ERROR, provider_message(_failure_text(event)))

                elif ev_type == "error":
                    # Not terminal: codex reports transient trouble this way too
                    # ("Reconnecting... 2/5" while falling back from WebSocket to HTTPS).
                    # Kept for the error message if the turn then ends without a result.
                    last_error = event.get("message") or "codex error"

            # Bounded: on Python 3.12 wait() only returns once every pipe is closed, and a
            # leftover process can hold stderr open. returncode is set at exit regardless.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(proc.wait()), _EXIT_WAIT_SEC)
            if proc.returncode is None:
                raise ApiError(ErrorCode.SDK_ERROR, "codex closed its output but did not exit")
            await _terminate(proc)  # sweep leftovers now so they cannot hold stderr open
            await _finish(stderr_task)
            if proc.returncode != 0:
                # What codex reported goes to the client; its stderr only to the log.
                reported = f": {provider_message(last_error)}" if last_error else ""
                tail = stderr_tail.text()
                raise ApiError(
                    ErrorCode.SDK_ERROR,
                    f"codex exited with code {proc.returncode}{reported}",
                    detail=f"stderr: {tail}" if tail.strip() else None,
                )
            if not completed:
                raise ApiError(
                    ErrorCode.SDK_ERROR,
                    provider_message(last_error) if last_error
                    else "codex ended without turn.completed",
                )

        except ApiError:
            raise
        except Exception as exc:
            raise ApiError(
                ErrorCode.SDK_ERROR,
                f"codex runner failed ({type(exc).__name__})",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            # Kill first so the pipes close, then reap the helpers; no step may raise,
            # or the rest of the cleanup would be skipped.
            await _terminate(proc)
            await _finish(stdin_task)
            await _finish(stderr_task)

    # aclosing makes closing this generator kill the process now, in this task.
    async with contextlib.aclosing(_stream()) as events:
        async for ev in events:
            yield ev


# stdout carries one JSON event per line; a single line can be huge (a command's
# aggregated output). Lines past this size are skipped, not fatal.
_MAX_EVENT_LINE_BYTES = 8 * 1024 * 1024
_STDERR_TAIL_BYTES = 4096
_TERM_GRACE_SEC = 2.0
_KILL_WAIT_SEC = 5.0
_EXIT_WAIT_SEC = 5.0


def _failure_text(event: dict) -> str:
    """turn.failed carries {"error": {"message": ...}}; tolerate a bare string too."""
    error = event.get("error")
    message = error.get("message") if isinstance(error, dict) else error
    return message if isinstance(message, str) and message.strip() else "turn failed"


def _toml_string(value: str) -> str:
    """A TOML basic string for a `-c key=value` override.

    JSON string escapes are valid TOML, except that ensure_ascii would emit surrogate
    pairs (TOML rejects them) and DEL, which TOML requires escaped, is left raw.
    """
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


async def _send_prompt(proc, prompt: str) -> None:
    assert proc.stdin is not None
    try:
        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass  # codex exited early; its exit code / stderr tell the story
    finally:
        proc.stdin.close()


async def _ndjson_lines(reader) -> AsyncIterator[bytes]:
    buf = bytearray()
    skipping = False
    while chunk := await reader.read(65536):
        start = 0
        while (newline := chunk.find(b"\n", start)) != -1:
            if not skipping:
                buf += chunk[start:newline]
                if buf.strip():
                    yield bytes(buf)
            buf.clear()
            skipping = False
            start = newline + 1
        if not skipping:
            buf += chunk[start:]
            if len(buf) > _MAX_EVENT_LINE_BYTES:
                log.warning("codex.event_line_skipped", over_bytes=_MAX_EVENT_LINE_BYTES)
                buf.clear()
                skipping = True
    if buf.strip() and not skipping:
        yield bytes(buf)


class _Tail:
    """The last `limit` bytes written — enough for an error message, bounded memory."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._buf = bytearray()

    def append(self, chunk: bytes) -> None:
        self._buf += chunk
        del self._buf[: max(0, len(self._buf) - self._limit)]

    def text(self, chars: int = 400) -> str:
        return self._buf.decode(errors="replace")[-chars:]


async def _drain(reader, tail: _Tail) -> None:
    while chunk := await reader.read(65536):
        tail.append(chunk)


async def _finish(task: asyncio.Task) -> None:
    """Wait (bounded) for a helper task; its failure never reaches the caller's cleanup.

    asyncio.wait never raises the task's exception, but does let a cancellation of the
    *calling* task through (e.g. drain's force-cancel) instead of swallowing it.
    """
    if not task.done():
        await asyncio.wait({task}, timeout=1.0)
    if not task.done():
        task.cancel()
        await asyncio.wait({task})
    if not task.cancelled():
        task.exception()  # mark retrieved; helpers only feed the error message


def _signal_group(pid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


async def _terminate(proc) -> None:
    """Stop codex's process group with bounded waits (never raises).

    The group holds the npm node wrapper, the native codex binary and anything else
    left in it. codex runs model shell commands in their own session (setsid), so
    those are outside the group — codex itself is responsible for them.
    """
    if proc.returncode is None:
        _signal_group(proc.pid, signal.SIGTERM)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(proc.wait()), _TERM_GRACE_SEC)
    # Always sweep: a member that ignored SIGTERM, or outlived a natural exit, goes now.
    # Safe: a process group id is never reused while any member is alive; an empty
    # group just yields ESRCH.
    _signal_group(proc.pid, signal.SIGKILL)
    if proc.returncode is None:
        # Bounded: on Python 3.12 wait() only returns once every pipe is closed.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(proc.wait()), _KILL_WAIT_SEC)


def _tool_use_from_item(item: dict) -> list[RunnerEvent]:
    item_type = item.get("type")
    tool_id = item.get("id")

    if item_type == "mcp_tool_call":
        return [ToolUseEvent(
            name=item.get("tool") or "unknown",
            args=item.get("arguments") or {},
            tool_use_id=tool_id,
        )]
    if item_type == "command_execution":
        return [ToolUseEvent(
            name="shell",
            args={"command": item.get("command") or ""},
            tool_use_id=tool_id,
        )]
    return []


def _tool_result_from_item(item: dict) -> RunnerEvent | None:
    item_type = item.get("type")
    tool_id = item.get("id")

    if item_type == "mcp_tool_call":
        ok = item.get("status") == "completed" and item.get("error") is None
        return ToolResultEvent(name=item.get("tool") or "unknown", ok=ok, tool_use_id=tool_id)
    if item_type == "command_execution":
        exit_code = item.get("exit_code")
        ok = isinstance(exit_code, int) and exit_code == 0
        return ToolResultEvent(name="shell", ok=ok, tool_use_id=tool_id)
    return None
