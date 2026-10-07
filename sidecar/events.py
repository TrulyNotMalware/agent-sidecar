"""The provider-neutral runner contract: what a runner yields and how it is called.

A runner (claude_runner.run_turn, codex_runner.run_turn) drives one CLI turn and
translates its output into these events. It has no timeout of its own and must close
its CLI when the generator is closed (sidecar.turn.Turn cancels the iterating task
once and relies on that).
"""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


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


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnSpec:
    """One turn as the request describes it, the same for every provider.

    The workspace (`cwd`) is not part of it: sidecar.turn.Turn enters the workspace
    after admission and hands it to the runner separately.
    """

    prompt: str
    system_prompt: str | None = None
    resume_session_id: str | None = None
    # The operator's static MCP servers (claude-only; codex reads its own config.toml).
    mcp_config_path: Path | None = None
    # The per-turn streamable-HTTP MCP server and the token that scopes it.
    mcp_server_url: str | None = None
    mcp_server_name: str = "domain-tools"
    turn_token: str | None = None
    # mode=stateless: the workspace is temporary and the session cannot be resumed.
    ephemeral: bool = False


class Runner(Protocol):
    """A provider's `run_turn` with its policy already bound (functools.partial).

    A generator, not just an iterator: closing it (aclose) must close the CLI.
    """

    def __call__(self, spec: TurnSpec, *, cwd: Path) -> AsyncGenerator[RunnerEvent, None]: ...
