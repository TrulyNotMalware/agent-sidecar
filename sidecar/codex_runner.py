import asyncio
import contextlib
import json
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


async def run_turn(
    *,
    prompt: str,
    cwd: Path,
    system_prompt: str | None,
    resume_session_id: str | None,
    mcp_config_path: Path | None,  # accepted for interface parity; Codex reads codex.toml from cwd
    timeout_sec: float,
) -> AsyncIterator[RunnerEvent]:
    effective_prompt = f"{system_prompt}\n\n{prompt}".strip() if system_prompt else prompt

    if len(effective_prompt.encode()) > _MAX_PROMPT_BYTES:
        raise ApiError(
            ErrorCode.BAD_REQUEST,
            f"combined prompt exceeds {_MAX_PROMPT_BYTES // 1000} KB limit for codex runner",
        )

    cmd = ["codex", "exec", "--json"]
    if resume_session_id:
        cmd += ["resume", resume_session_id, effective_prompt]
    else:
        cmd.append(effective_prompt)

    async def _stream() -> AsyncIterator[RunnerEvent]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            limit=1024 * 1024,  # 1 MiB per line — guards against LimitOverrunError
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
