import asyncio
import json
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from .observability.logging import get_logger

log = get_logger("sidecar.mcp")

# The CLI names MCP tools `mcp__<server>__<tool>` with every character outside
# [A-Za-z0-9_-] replaced by "_", and matches permission rules against that form. A rule
# must use the same normalization — "v1.2" only matches as "v1_2" — and it also keeps
# "," or " " out of the comma/space-separated --allowedTools ("x,Bash" → "x_Bash").
_NOT_IN_TOOL_NAME = re.compile(r"[^A-Za-z0-9_-]")


def mcp_tool_prefix(server_name: str) -> str:
    """The permission rule covering every tool of `server_name`."""
    return f"mcp__{_NOT_IN_TOOL_NAME.sub('_', server_name)}"


def mcp_tool_name(server_name: str, tool_name: str) -> str:
    """A tool's full name as claude reports it; codex events are named the same way."""
    return f"{mcp_tool_prefix(server_name)}__{_NOT_IN_TOOL_NAME.sub('_', tool_name)}"


def static_mcp_servers(static_config_path: Path | None) -> dict[str, Any]:
    """The `mcpServers` object of the operator's mcp.json.

    Empty when unset, and empty with a warning when the file is unreadable or not
    shaped like an mcp.json: a broken static config must not fail every turn.
    """
    if static_config_path is None:
        return {}
    try:
        parsed = json.loads(static_config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # missing, unreadable, not JSON
        log.warning("mcp.static_config_unreadable", error_type=type(exc).__name__)
        return {}
    servers = (parsed.get("mcpServers") or {}) if isinstance(parsed, dict) else None
    if not isinstance(servers, dict):
        log.warning("mcp.static_config_unreadable", error_type="TypeError")
        return {}
    return servers


def static_mcp_server_names(static_config_path: Path | None) -> list[str]:
    """Server names in the operator's mcp.json (empty if unset or unreadable)."""
    return [str(name) for name in static_mcp_servers(static_config_path)]


def per_turn_mcp_servers(
    static_servers: Mapping[str, Any],
    *,
    server_name: str,
    server_url: str | None,
    turn_token: str | None,
) -> dict[str, Any] | None:
    """The `mcpServers` of a turn that reaches a per-turn MCP server, else None.

    With a server url and a turn token: the operator's static servers plus a
    streamable-HTTP entry carrying the token as a bearer; the per-turn entry wins on
    a name collision. Without both there is nothing to merge, and the caller passes
    the static mcp.json to the CLI as a path, unchanged.
    """
    if server_url is None or turn_token is None:
        return None
    servers = dict(static_servers)
    servers[server_name] = {
        "type": "http",
        "url": server_url,
        "headers": {"Authorization": f"Bearer {turn_token}"},
    }
    return servers


@asynccontextmanager
async def private_mcp_config(servers: dict[str, Any]) -> AsyncIterator[Path]:
    """Write `servers` as an mcp.json readable only by this user; delete it on exit.

    The SDK serialises a dict `mcp_servers` into `--mcp-config '<json>'` on the CLI's
    argv, where the per-turn bearer is visible to any process via `ps`. Handing the
    SDK a file path keeps it off argv. The file lives in a fresh 0700 directory
    outside the turn workspace, so the agent's cwd-scoped tools do not list it.
    """
    async with private_file("mcp.json", json.dumps({"mcpServers": servers})) as path:
        yield path


@asynccontextmanager
async def private_file(name: str, content: str) -> AsyncIterator[Path]:
    """Write `content` to a file only this user can read; delete it on exit.

    For what must not go on a CLI's argv, where every process can read it via `ps`
    (and Linux caps a single argument at 128 KiB). The file lives in a fresh 0700
    directory outside the turn workspace, so the agent's cwd-scoped tools do not
    list it.
    """
    directory = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="claude-sidecar-"))
    try:
        yield await asyncio.to_thread(_write_private, directory / name, content)
    finally:
        await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=True)


def _write_private(path: Path, content: str) -> Path:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return path
