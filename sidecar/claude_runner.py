import asyncio
import collections
import contextlib
import os
import re
from collections.abc import AsyncGenerator, Callable, Iterable, Iterator
from dataclasses import dataclass, field
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
    mcp_tool_prefix,
    per_turn_mcp_servers,
    private_file,
    private_mcp_config,
    static_mcp_servers,
)
from .observability.logging import get_logger
from .session import is_session_id, remove_paths

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


# The CLI names a project directory after the cwd, every character outside
# [A-Za-z0-9] replaced by "-"; a longer name is cut and suffixed with a hash.
_PROJECT_NAME_MAX = 200
_NOT_IN_PROJECT_NAME = re.compile(r"[^A-Za-z0-9]")


def claude_config_dir() -> Path:
    """Where the CLI keeps its state: $CLAUDE_CONFIG_DIR, else ~/.claude."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured) if configured else Path.home() / ".claude"


def claude_project_dir(cwd: Path) -> Path | None:
    """The directory the CLI keeps the transcripts of sessions run in `cwd` in.

    None when the name would be longer than the CLI's limit: it then ends in a hash
    of the path, and a guess by prefix could match another workspace's directory.
    """
    # process.cwd() as the CLI sees it: absolute, symlinks resolved.
    name = _NOT_IN_PROJECT_NAME.sub("-", str(cwd.resolve()))
    return claude_config_dir() / "projects" / name if len(name) <= _PROJECT_NAME_MAX else None


def claude_session_files(cwd: Path, session_ids: Iterable[str]) -> list[Path]:
    """What the CLI stored for the sessions run in `cwd` (best effort, CLI internals).

    The project directory of `cwd` (transcripts, subagent logs) and, for each
    session id, its entries anywhere one level down in the config dir
    (`file-history/<id>`, `todos/<id>-…`, `session-env/<id>`, a resumed session's
    transcript in another project directory, …). Ids that are not UUIDs are skipped.
    """
    config = claude_config_dir()
    project = claude_project_dir(cwd)
    found: list[Path] = [] if project is None else [project]
    # Past the name limit only the session ids can find the project's entries.
    for sid in filter(is_session_id, session_ids):
        found += config.glob(f"*/{sid}*")
        found += config.glob(f"projects/*/{sid}*")
    unique = list(dict.fromkeys(found))
    # A transcript inside the project directory goes with the directory.
    return [p for p in unique if not any(p != q and p.is_relative_to(q) for q in unique)]


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
    anthropic_api_key: str | None = field(default=None, repr=False)


async def run_turn(
    spec: TurnSpec, *, cwd: Path, policy: ClaudePolicy | None = None
) -> AsyncGenerator[RunnerEvent, None]:
    """Drive one Claude turn via the Agent SDK and yield internal events.

    No timeout here: the caller bounds the turn (sidecar.turn.Turn) and cancels the
    task that iterates this generator exactly once, so the SDK can close the CLI.

    `spec.ephemeral` (mode=stateless): once the CLI has exited, what it stored for
    the turn under $CLAUDE_CONFIG_DIR is removed — its project directory (named after
    the temp cwd) and the entries named after its session id. The CLI has no way to
    not store it: --no-session-persistence is --print-only, not SDK mode. (Resuming
    such a session is refused anyway: its id is recorded under no sessionKey.)
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
    if spec.token_streaming:
        options_kwargs["include_partial_messages"] = True
    if spec.model is not None:
        options_kwargs["model"] = spec.model
    if spec.max_turns is not None:
        options_kwargs["max_turns"] = spec.max_turns
    if spec.max_budget_usd is not None:
        options_kwargs["max_budget_usd"] = spec.max_budget_usd
    if spec.output_schema is not None:
        # Not secret, so argv (--json-schema) is fine; its size is bounded by the request.
        options_kwargs["output_format"] = {"type": "json_schema", "schema": spec.output_schema}
    static_servers = await asyncio.to_thread(static_mcp_servers, spec.mcp_config_path)
    merged_servers = per_turn_mcp_servers(
        static_servers,
        server_name=spec.mcp_server_name,
        server_url=spec.mcp_server_url,
        turn_token=spec.turn_token,
    )
    # Headless runs have nobody to approve tool prompts, so MCP servers the operator
    # configured must be pre-allowed ("mcp__<server>" covers every tool it exposes).
    # For the per-turn server, authorization is enforced server-side via the turn token.
    approved = [*policy.allowed_tools, *map(mcp_tool_prefix, static_servers)]
    if merged_servers is not None:
        approved.append(mcp_tool_prefix(spec.mcp_server_name))
    if approved:
        options_kwargs["allowed_tools"] = list(dict.fromkeys(approved))

    async with contextlib.AsyncExitStack() as cleanup:
        session_ids: set[str] = set()
        if spec.ephemeral:
            # Registered first, so it runs last: after the CLI has exited.
            cleanup.push_async_callback(_forget_stateless_turn, cwd, session_ids)
        if spec.system_prompt is not None:
            # A file, not `--system-prompt <text>` on argv (visible via `ps`, capped at
            # 128 KiB per argument on Linux).
            prompt_file = private_file("system-prompt.md", spec.system_prompt)
            path = await cleanup.enter_async_context(prompt_file)
            options_kwargs["system_prompt"] = {"type": "file", "path": str(path)}
        if merged_servers is not None:
            # Carries the turn token: pass a private file, never inline JSON on argv.
            mcp_path = await cleanup.enter_async_context(private_mcp_config(merged_servers))
            options_kwargs["mcp_servers"] = str(mcp_path)
        elif spec.mcp_config_path is not None:
            options_kwargs["mcp_servers"] = str(spec.mcp_config_path)
        run = _run(
            options_kwargs,
            prompt=spec.prompt,
            token_streaming=spec.token_streaming,
            structured=spec.output_schema is not None,
        )
        async with contextlib.aclosing(run) as events:
            async for ev in events:
                if isinstance(ev, SessionEvent):
                    session_ids.add(ev.session_id)
                yield ev


async def _forget_stateless_turn(cwd: Path, session_ids: set[str]) -> None:
    """Remove what the CLI stored for a stateless turn; a failure is logged, not raised."""

    def forget() -> int:
        return remove_paths(claude_session_files(cwd, session_ids))

    try:
        await asyncio.to_thread(forget)
    except OSError as exc:
        log.warning("claude.stateless_transcript_not_removed", error_type=type(exc).__name__)


@dataclass(slots=True)
class _Progress:
    """What the SDK's messages have told us so far."""

    final_text_parts: list[str] = field(default_factory=list)
    pending_tool_names: dict[str, str] = field(default_factory=dict)  # tool_use_id → name
    session_emitted: bool = False
    # An error result is raised only after the SDK stream ends: raising inside the loop
    # would aclose() the public query() at its yield, and its inner generator (which
    # closes the CLI) would be finalized later in a detached task — releasing the
    # turn's reservation while the CLI still runs.
    result_error: ApiError | None = None


def _stderr_sink() -> tuple[Callable[[str], None], collections.deque[str]]:
    """A stderr callback for the SDK, and the tail of lines it keeps.

    Setting a callback is what makes the SDK pipe the CLI's stderr at all (otherwise it
    goes straight to the pod log, unscrubbed). Each line is logged — scrubbed, with the
    turn's id — and the tail explains a failed CLI.
    """
    tail: collections.deque[str] = collections.deque(maxlen=_STDERR_TAIL_LINES)
    lines = 0

    def on_stderr(line: str) -> None:
        nonlocal lines
        line = line[:_STDERR_LINE_CHARS]
        tail.append(line)
        lines += 1
        if lines <= _STDERR_LOG_LINES:
            log.info("claude.stderr", line=line)
        elif lines == _STDERR_LOG_LINES + 1:
            log.warning("claude.stderr_not_logged", after_lines=_STDERR_LOG_LINES)

    return on_stderr, tail


def _done_event(message: object, final_text_parts: list[str]) -> DoneEvent:
    """The terminal event for a successful result message."""
    result = getattr(message, "result", None)
    usage = _extract_usage(message)
    cost = getattr(message, "total_cost_usd", None)
    num_turns = getattr(message, "num_turns", None)
    stop_reason = getattr(message, "stop_reason", None)
    return DoneEvent(
        final_text=result if isinstance(result, str) and result else "".join(final_text_parts),
        input_tokens=int(usage.get("input_tokens", 0) or 0),
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        cache_read_input_tokens=usage.get("cache_read_input_tokens"),
        cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
        num_turns=num_turns if isinstance(num_turns, int) else None,
        stop_reason=stop_reason if isinstance(stop_reason, str) and stop_reason else None,
        structured_output=getattr(message, "structured_output", None),
    )


async def _run(
    options_kwargs: dict[str, Any],
    *,
    prompt: str,
    token_streaming: bool = False,
    structured: bool = False,
) -> AsyncGenerator[RunnerEvent, None]:
    # Lazy import keeps the rest of the app usable without the SDK (e.g. auth/health tests).
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        Message,
        ResultError,
        ResultMessage,
        StreamEvent,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
        query,
    )

    on_stderr, stderr_tail = _stderr_sink()
    options = ClaudeAgentOptions(**options_kwargs, stderr=on_stderr)
    progress = _Progress()

    def translate(message: Message) -> Iterator[RunnerEvent]:
        """The runner events one SDK message amounts to; records what later ones need."""
        if not progress.session_emitted and (sid := _extract_session_id(message)):
            progress.session_emitted = True
            yield SessionEvent(session_id=sid)

        if isinstance(message, AssistantMessage):
            if getattr(message, "error", None) is not None:
                # An API failure the CLI reports as a synthetic assistant message. It is
                # not the model's answer: the error result that follows becomes the
                # terminal frame (scrubbed).
                return
            for block in message.content:
                if isinstance(block, TextBlock):
                    progress.final_text_parts.append(block.text)
                    if not token_streaming:  # else already sent, delta by delta
                        yield TextEvent(delta=block.text)
                elif isinstance(block, ToolUseBlock):
                    progress.pending_tool_names[block.id] = block.name
                    yield ToolUseEvent(
                        name=block.name, args=dict(block.input or {}), tool_use_id=block.id
                    )
        elif isinstance(message, StreamEvent):
            if (delta := _text_delta(message.event)) is not None:
                yield TextEvent(delta=delta)
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    yield ToolResultEvent(
                        name=progress.pending_tool_names.get(block.tool_use_id, "unknown"),
                        ok=not bool(getattr(block, "is_error", False)),
                        tool_use_id=block.tool_use_id,
                    )
        elif isinstance(message, ResultMessage):
            if message.is_error:
                progress.result_error = _result_error(
                    message.errors, message.result, message.subtype
                )
            elif structured and message.structured_output is None:
                # The CLI fails the turn itself when no attempt matched the schema;
                # this only guards against a result that silently lacks it.
                progress.result_error = ApiError(
                    ErrorCode.SDK_ERROR, "claude returned no structured output"
                )
            else:
                yield _done_event(message, progress.final_text_parts)

    # aclosing: closing this generator must close the SDK's query() (and so the CLI)
    # right away, in this task — not whenever the GC gets to it. The SDK types query()
    # as an AsyncIterator; it must really be a generator (nothing runs until iterated).
    stream = query(prompt=prompt, options=options)
    if not isinstance(stream, AsyncGenerator):
        raise TypeError("claude_agent_sdk.query() no longer returns an async generator")
    try:
        async with contextlib.aclosing(stream) as messages:
            async for message in messages:
                if progress.result_error is None:
                    for ev in translate(message):
                        yield ev
    except ApiError:
        raise
    except Exception as exc:
        # After an error result the CLI exits non-zero and the SDK raises ResultError
        # restating it — also when no message came first (e.g. a resume refused during
        # the SDK's initialize).
        if progress.result_error is None and isinstance(exc, ResultError):
            progress.result_error = _result_error(exc.errors, exc.result, exc.subtype)
        if progress.result_error is None:
            raise _cli_failure(exc, "\n".join(stderr_tail)) from exc
        raise progress.result_error from exc
    if progress.result_error is not None:
        raise progress.result_error


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


def _text_delta(event: dict[str, Any]) -> str | None:
    """The text a raw API stream event adds (include_partial_messages), if any."""
    if event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return None
    text = delta.get("text")
    return text if isinstance(text, str) and text else None


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
