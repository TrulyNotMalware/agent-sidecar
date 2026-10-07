import collections
import contextlib
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import PermissionMode
from .errors import ApiError, ErrorCode, provider_message
from .events import (
    DoneEvent,
    RunnerEvent,
    SessionEvent,
    TextEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnSpec,
)
from .mcp import (
    build_mcp_servers,
    mcp_tool_prefix,
    private_file,
    private_mcp_config,
    static_mcp_server_names,
)
from .observability.logging import get_logger

# The SDK launches the CLI with the sidecar's full environment and only lets options
# add or override keys. Blank the secrets that are the sidecar's own business so the
# agent's tools cannot read them from its environment. ClaudePolicy.withheld_env adds
# more names (e.g. the CODEX_ENV_PASSTHROUGH values).
_WITHHELD_FROM_CLI = ("BEARER_SECRET", "OPENAI_API_KEY")
_STDERR_TAIL_LINES = 40
_STDERR_LINE_CHARS = 2000  # the SDK hands over lines of up to ~1 MB
_STDERR_LOG_LINES = 200  # per turn; the tail still reaches error_detail
_STDERR_DETAIL_CHARS = 2000  # of the stderr tail quoted in error_detail

log = get_logger("sidecar.claude")


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudePolicy:
    """The agent policy for claude turns, built once from Settings (see _get_runner).

    Explicit rather than inherited from the CLI's defaults:
    - tools: built-in toolset (None = the CLI's default set, () = none).
    - permission_mode "dontAsk": anything that would prompt is denied (headless),
      unless pre-approved via allowed_tools. MCP servers the operator configured are
      pre-approved automatically.
    - setting_sources () + --strict-mcp-config: no ~/.claude or project settings,
      hooks, plugins or MCP servers leak in from wherever the sidecar runs.
    """

    tools: tuple[str, ...] | None = None
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()  # deny beats allow
    permission_mode: PermissionMode = "dontAsk"
    setting_sources: tuple[str, ...] = ()
    restricted: bool = False
    # Blanked in the CLI's environment on top of _WITHHELD_FROM_CLI.
    withheld_env: tuple[str, ...] = ()
    # Settings' key (which may come from .env, not the environment the CLI inherits).
    anthropic_api_key: str | None = None


async def run_turn(
    spec: TurnSpec, *, cwd: Path, policy: ClaudePolicy | None = None
) -> AsyncGenerator[RunnerEvent, None]:
    """Drive one Claude turn via the Agent SDK and yield internal events.

    No timeout here: the caller bounds the turn (sidecar.turn.Turn) and cancels the
    task that iterates this generator exactly once, so the SDK can close the CLI.

    `spec.ephemeral` is interface parity with codex: claude's transcript is keyed by
    the (deleted) temp cwd, so a stateless turn cannot be resumed anyway; the file
    itself stays under $CLAUDE_CONFIG_DIR/projects (default ~/.claude/projects;
    --no-session-persistence is --print-only, not SDK mode).
    """
    policy = ClaudePolicy() if policy is None else policy
    options_kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "env": {
            **dict.fromkeys((*_WITHHELD_FROM_CLI, *policy.withheld_env), ""),
            **({"ANTHROPIC_API_KEY": policy.anthropic_api_key} if policy.anthropic_api_key else {}),
        },
        "permission_mode": policy.permission_mode,
        "setting_sources": list(policy.setting_sources),
        "extra_args": {
            "strict-mcp-config": None,
            **({"restricted": None} if policy.restricted else {}),
        },
    }
    if policy.tools is not None:
        options_kwargs["tools"] = list(policy.tools)
    if policy.disallowed_tools:
        options_kwargs["disallowed_tools"] = list(policy.disallowed_tools)
    if spec.resume_session_id:
        options_kwargs["resume"] = spec.resume_session_id
    mcp_servers = build_mcp_servers(
        static_config_path=spec.mcp_config_path,
        server_name=spec.mcp_server_name,
        server_url=spec.mcp_server_url,
        turn_token=spec.turn_token,
    )
    # Headless runs have nobody to approve tool prompts, so MCP servers the operator
    # configured must be pre-allowed ("mcp__<server>" covers every tool it exposes).
    # For the per-turn server, authorization is enforced server-side via the turn token.
    approved = [
        *policy.allowed_tools,
        *map(mcp_tool_prefix, static_mcp_server_names(spec.mcp_config_path)),
    ]
    if spec.mcp_server_url is not None and spec.turn_token is not None:
        approved.append(mcp_tool_prefix(spec.mcp_server_name))
    if approved:
        options_kwargs["allowed_tools"] = list(dict.fromkeys(approved))

    with contextlib.ExitStack() as cleanup:
        if spec.system_prompt is not None:
            # A file, not `--system-prompt <text>` on argv (visible via `ps`, capped at
            # 128 KiB per argument on Linux).
            path = cleanup.enter_context(private_file("system-prompt.md", spec.system_prompt))
            options_kwargs["system_prompt"] = {"type": "file", "path": str(path)}
        if isinstance(mcp_servers, dict):
            # Carries the turn token: pass a private file, never inline JSON on argv.
            mcp_servers = str(cleanup.enter_context(private_mcp_config(mcp_servers)))
        if mcp_servers is not None:
            options_kwargs["mcp_servers"] = mcp_servers
        async with contextlib.aclosing(_run(options_kwargs, prompt=spec.prompt)) as events:
            async for ev in events:
                yield ev


async def _run(options_kwargs: dict[str, Any], *, prompt: str) -> AsyncGenerator[RunnerEvent, None]:
    # Lazy import keeps the rest of the app usable without the SDK (e.g. auth/health tests).
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultError,
        ResultMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
        query,
    )

    # Setting a callback is what makes the SDK pipe the CLI's stderr at all (otherwise it
    # goes straight to the pod log, unscrubbed). Each line is logged — scrubbed, with the
    # turn's id — and the tail explains a failed CLI.
    stderr_tail: collections.deque[str] = collections.deque(maxlen=_STDERR_TAIL_LINES)
    stderr_lines = 0

    def on_stderr(line: str) -> None:
        nonlocal stderr_lines
        line = line[:_STDERR_LINE_CHARS]
        stderr_tail.append(line)
        stderr_lines += 1
        if stderr_lines <= _STDERR_LOG_LINES:
            log.info("claude.stderr", line=line)
        elif stderr_lines == _STDERR_LOG_LINES + 1:
            log.warning("claude.stderr_not_logged", after_lines=_STDERR_LOG_LINES)

    options = ClaudeAgentOptions(**options_kwargs, stderr=on_stderr)
    final_text_parts: list[str] = []
    pending_tool_names: dict[str, str] = {}
    session_emitted = False
    # An error result is raised only after the SDK stream ends: raising inside the loop
    # would aclose() the public query() at its yield, and its inner generator (which
    # closes the CLI) would be finalized later in a detached task — releasing the
    # turn's reservation while the CLI still runs.
    result_error: ApiError | None = None

    try:
        # aclosing: closing this generator must close the SDK's query() (and so
        # the CLI) right away, in this task — not whenever the GC gets to it. The
        # SDK types query() as an AsyncIterator; it must really be a generator.
        stream = query(prompt=prompt, options=options)
        if not isinstance(stream, AsyncGenerator):
            raise TypeError("claude_agent_sdk.query() no longer returns an async generator")
        async with contextlib.aclosing(stream) as messages:
            async for message in messages:
                if result_error is not None:
                    continue
                if not session_emitted:
                    sid = _extract_session_id(message)
                    if sid:
                        session_emitted = True
                        yield SessionEvent(session_id=sid)

                if isinstance(message, AssistantMessage):
                    if getattr(message, "error", None) is not None:
                        # An API failure the CLI reports as a synthetic assistant message.
                        # It is not the model's answer: the error result that follows
                        # becomes the terminal frame (scrubbed).
                        continue
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
                                tool_name = pending_tool_names.get(block.tool_use_id, "unknown")
                                yield ToolResultEvent(
                                    name=tool_name,
                                    ok=not bool(getattr(block, "is_error", False)),
                                    tool_use_id=block.tool_use_id,
                                )
                elif isinstance(message, ResultMessage):
                    if message.is_error:
                        result_error = _result_error(
                            message.errors, message.result, message.subtype
                        )
                        continue
                    final_text = getattr(message, "result", None) or "".join(final_text_parts)
                    usage = _extract_usage(message)
                    yield DoneEvent(
                        final_text=final_text,
                        input_tokens=int(usage.get("input_tokens", 0) or 0),
                        output_tokens=int(usage.get("output_tokens", 0) or 0),
                        cache_read_input_tokens=usage.get("cache_read_input_tokens"),
                        cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
                    )
    except ApiError:
        raise
    except Exception as exc:
        # After an error result the CLI exits non-zero and the SDK raises ResultError
        # restating it — also when no message came first (e.g. a resume refused during
        # the SDK's initialize).
        if result_error is None and isinstance(exc, ResultError):
            result_error = _result_error(exc.errors, exc.result, exc.subtype)
        if result_error is None:
            raise _cli_failure(exc, "\n".join(stderr_tail)) from exc
        raise result_error from exc
    if result_error is not None:
        raise result_error


def _result_error(errors: list[str] | None, result: str | None, subtype: str | None) -> ApiError:
    """An error result the CLI reported (API error, max turns, …): the client's to see."""
    reported = "; ".join(e for e in errors or () if isinstance(e, str) and e.strip())
    text = reported or result or subtype or "claude reported an error"
    return ApiError(ErrorCode.SDK_ERROR, provider_message(text))


def _cli_failure(exc: Exception, stderr: str) -> ApiError:
    """The CLI or the SDK itself failed: the details (stderr, …) go to the log only."""
    exit_code = getattr(exc, "exit_code", None)
    code = f", exit code {exit_code}" if exit_code is not None else ""
    # CLIJSONDecodeError's text quotes the CLI's output line — conversation content.
    cause = exc.original_error if hasattr(exc, "original_error") and hasattr(exc, "line") else exc
    detail = f"{type(exc).__name__}: {cause}"
    if stderr.strip():
        detail += f"; stderr: {stderr[-_STDERR_DETAIL_CHARS:]}"
    return ApiError(
        ErrorCode.SDK_ERROR, f"claude CLI failed ({type(exc).__name__}{code})", detail=detail
    )


def _extract_session_id(message: object) -> str | None:
    sid = getattr(message, "session_id", None)
    if not sid:
        data = getattr(message, "data", None)
        sid = data.get("session_id") if isinstance(data, dict) else None
    return sid if isinstance(sid, str) and sid else None


def _extract_usage(message: object) -> dict[str, Any]:
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
