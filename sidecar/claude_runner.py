import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ApiError, ErrorCode
from .mcp import build_mcp_servers


@dataclass(frozen=True)
class SessionEvent:
    session_id: str


@dataclass(frozen=True)
class TextEvent:
    delta: str


@dataclass(frozen=True)
class ToolUseEvent:
    name: str
    args: dict[str, Any]
    tool_use_id: str | None


@dataclass(frozen=True)
class ToolResultEvent:
    name: str
    ok: bool
    tool_use_id: str | None


@dataclass(frozen=True)
class DoneEvent:
    final_text: str
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int | None
    cache_creation_input_tokens: int | None


RunnerEvent = SessionEvent | TextEvent | ToolUseEvent | ToolResultEvent | DoneEvent


async def run_turn(
    *,
    prompt: str,
    cwd: Path,
    system_prompt: str | None,
    resume_session_id: str | None,
    mcp_config_path: Path | None,
    mcp_server_url: str | None = None,
    mcp_server_name: str = "codecompanion",
    turn_token: str | None = None,
    timeout_sec: float,
) -> AsyncIterator[RunnerEvent]:
    """Drive one Claude turn via the Agent SDK and yield internal events.

    SDK imports are lazy so the rest of the app stays usable without the SDK
    available (e.g. unit tests for auth/health).
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
        query,
    )

    options_kwargs: dict[str, Any] = {"cwd": str(cwd)}
    if system_prompt is not None:
        options_kwargs["system_prompt"] = system_prompt
    if resume_session_id:
        options_kwargs["resume"] = resume_session_id
    mcp_servers = build_mcp_servers(
        static_config_path=mcp_config_path,
        server_name=mcp_server_name,
        server_url=mcp_server_url,
        turn_token=turn_token,
    )
    if mcp_servers is not None:
        options_kwargs["mcp_servers"] = mcp_servers
    if mcp_server_url is not None and turn_token is not None:
        # Headless runs have nobody to approve tool prompts, so the scoped server's tools
        # must be pre-allowed ("mcp__<server>" covers every tool it exposes). Authorization
        # is enforced server-side per call via the turn token; this only unblocks the SDK.
        options_kwargs["allowed_tools"] = [f"mcp__{mcp_server_name}"]

    options = ClaudeAgentOptions(**options_kwargs)

    async def _stream() -> AsyncIterator[RunnerEvent]:
        final_text_parts: list[str] = []
        pending_tool_names: dict[str, str] = {}
        session_emitted = False

        try:
            async for message in query(prompt=prompt, options=options):
                if not session_emitted:
                    sid = _extract_session_id(message)
                    if sid:
                        session_emitted = True
                        yield SessionEvent(session_id=sid)

                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            final_text_parts.append(block.text)
                            yield TextEvent(delta=block.text)
                        elif isinstance(block, ToolUseBlock):
                            pending_tool_names[block.id] = block.name
                            yield ToolUseEvent(
                                name=block.name,
                                args=dict(block.input or {}),
                                tool_use_id=block.id,
                            )
                elif isinstance(message, UserMessage):
                    if isinstance(message.content, list):
                        for block in message.content:
                            if isinstance(block, ToolResultBlock):
                                tool_name = pending_tool_names.get(
                                    block.tool_use_id, "unknown"
                                )
                                yield ToolResultEvent(
                                    name=tool_name,
                                    ok=not bool(getattr(block, "is_error", False)),
                                    tool_use_id=block.tool_use_id,
                                )
                elif isinstance(message, ResultMessage):
                    if message.is_error:
                        errs = getattr(message, "errors", None) or [
                            getattr(message, "stop_reason", None) or "claude reported error"
                        ]
                        raise ApiError(ErrorCode.SDK_ERROR, "; ".join(str(e) for e in errs))
                    final_text = (
                        getattr(message, "result", None) or "".join(final_text_parts)
                    )
                    usage = _extract_usage(message)
                    yield DoneEvent(
                        final_text=final_text,
                        input_tokens=int(usage.get("input_tokens", 0) or 0),
                        output_tokens=int(usage.get("output_tokens", 0) or 0),
                        cache_read_input_tokens=usage.get("cache_read_input_tokens"),
                        cache_creation_input_tokens=usage.get(
                            "cache_creation_input_tokens"
                        ),
                    )
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError(ErrorCode.SDK_ERROR, f"{type(exc).__name__}: {exc}") from exc

    try:
        async with asyncio.timeout(timeout_sec):
            async for ev in _stream():
                yield ev
    except TimeoutError as exc:
        raise ApiError(ErrorCode.TIMEOUT, f"turn exceeded {timeout_sec}s") from exc


def _extract_session_id(message: Any) -> str | None:
    sid = getattr(message, "session_id", None)
    if sid:
        return sid
    data = getattr(message, "data", None)
    if isinstance(data, dict):
        return data.get("session_id")
    return None


def _extract_usage(message: Any) -> dict[str, Any]:
    usage = getattr(message, "usage", None)
    if isinstance(usage, dict):
        return usage
    if usage is None:
        return {}
    return {
        k: getattr(usage, k, None)
        for k in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
    }
