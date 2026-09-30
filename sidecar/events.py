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


class Runner(Protocol):
    """`run_turn` with its provider-specific options already bound (functools.partial).

    A generator, not just an iterator: closing it (aclose) must close the CLI.
    """

    def __call__(
        self,
        *,
        prompt: str,
        cwd: Path,
        system_prompt: str | None,
        resume_session_id: str | None,
        mcp_config_path: Path | None,
        mcp_server_url: str | None,
        mcp_server_name: str,
        turn_token: str | None,
        ephemeral: bool,
    ) -> AsyncGenerator[RunnerEvent, None]: ...
