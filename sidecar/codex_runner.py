import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

from .claude_runner import (
    DoneEvent,
    RunnerEvent,
    SessionEvent,
    TextEvent,
    ToolResultEvent,
    ToolUseEvent,
)
from .errors import ApiError, ErrorCode

# Prompts are passed as argv; guard against ARG_MAX exhaustion.
_MAX_PROMPT_BYTES = 100_000


async def ensure_codex_auth(auth_path: Path | None = None) -> bool:
    """Materialize the codex auth state from OPENAI_API_KEY.

    codex-cli does not send OPENAI_API_KEY from the environment at request
    time; the key must be registered once via `codex login --with-api-key`,
    which writes ~/.codex/auth.json. No-op when the auth file already exists
    (subscription mode) or no key is present. Returns True when an auth file
    is available afterwards.
    """
    path = auth_path or (Path.home() / ".codex" / "auth.json")
    if path.exists():
        return True
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return False
    proc = await asyncio.create_subprocess_exec(
        "codex",
        "login",
        "--with-api-key",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate(key.encode())
    return proc.returncode == 0 and path.exists()


_MCP_TOKEN_ENV_VAR = "CODECOMPANION_MCP_TOKEN"


async def run_turn(
    *,
    prompt: str,
    cwd: Path,
    system_prompt: str | None,
    resume_session_id: str | None,
    mcp_config_path: Path | None,  # accepted for interface parity; Codex reads codex.toml from cwd
    mcp_server_url: str | None = None,
    mcp_server_name: str = "codecompanion",
    turn_token: str | None = None,
    timeout_sec: float,
) -> AsyncIterator[RunnerEvent]:
    effective_prompt = f"{system_prompt}\n\n{prompt}".strip() if system_prompt else prompt

    if len(effective_prompt.encode()) > _MAX_PROMPT_BYTES:
        raise ApiError(
            ErrorCode.BAD_REQUEST,
            f"combined prompt exceeds {_MAX_PROMPT_BYTES // 1000} KB limit for codex runner",
        )

    # Session workspaces are plain scratch dirs; without the flag `codex exec`
    # refuses to run outside a trusted git repository.
    cmd = ["codex", "exec", "--json", "--skip-git-repo-check"]

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
        cmd += [
            "-c",
            f'mcp_servers.{mcp_server_name}.url="{mcp_server_url}"',
            "-c",
            f'mcp_servers.{mcp_server_name}.bearer_token_env_var="{_MCP_TOKEN_ENV_VAR}"',
            "-c",
            f'mcp_servers.{mcp_server_name}.default_tools_approval_mode="approve"',
        ]

    if resume_session_id:
        cmd += ["resume", resume_session_id, effective_prompt]
    else:
        cmd.append(effective_prompt)

    turn_env = {**os.environ, _MCP_TOKEN_ENV_VAR: turn_token} if mcp_scoped else None

    async def _stream() -> AsyncIterator[RunnerEvent]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            # An inherited pipe makes codex wait for "additional input from stdin".
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            limit=1024 * 1024,  # 1 MiB per line — guards against LimitOverrunError
            **({"env": turn_env} if turn_env is not None else {}),
        )

        stderr_chunks: list[bytes] = []

        async def _drain_stderr() -> None:
            assert proc.stderr is not None
            async for chunk in proc.stderr:
                stderr_chunks.append(chunk)

        stderr_task = asyncio.create_task(_drain_stderr())
        final_text_parts: list[str] = []

        try:
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
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
                    usage = event.get("usage") or {}
                    yield DoneEvent(
                        final_text="".join(final_text_parts),
                        input_tokens=int(usage.get("input_tokens") or 0),
                        output_tokens=int(usage.get("output_tokens") or 0),
                        cache_read_input_tokens=usage.get("cached_input_tokens"),
                        cache_creation_input_tokens=None,
                    )

                elif ev_type == "turn.failed":
                    err = event.get("error") or {}
                    raise ApiError(ErrorCode.SDK_ERROR, err.get("message") or "turn failed")

                elif ev_type == "error":
                    raise ApiError(ErrorCode.SDK_ERROR, event.get("message") or "codex error")

            await proc.wait()
            if proc.returncode != 0:
                tail = b"".join(stderr_chunks[-20:]).decode(errors="replace")
                detail = f": {tail[:400]}" if tail.strip() else ""
                raise ApiError(
                    ErrorCode.SDK_ERROR,
                    f"codex exited with code {proc.returncode}{detail}",
                )

        except ApiError:
            raise
        except Exception as exc:
            raise ApiError(ErrorCode.SDK_ERROR, f"{type(exc).__name__}: {exc}") from exc
        finally:
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    try:
        async with asyncio.timeout(timeout_sec):
            async for ev in _stream():
                yield ev
    except TimeoutError as exc:
        raise ApiError(ErrorCode.TIMEOUT, f"turn exceeded {timeout_sec}s") from exc


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
