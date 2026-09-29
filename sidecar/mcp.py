import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
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


def static_mcp_server_names(static_config_path: Path | None) -> list[str]:
    """Server names in the operator's mcp.json (empty if unset or unreadable)."""
    if static_config_path is None:
        return []
    try:
        parsed = json.loads(static_config_path.read_text(encoding="utf-8"))
        servers = parsed.get("mcpServers") or {}
        if not isinstance(servers, dict):
            raise TypeError("mcpServers must be an object")
        return [str(name) for name in servers]
    except Exception as exc:  # noqa: BLE001
        log.warning("mcp.static_config_unreadable", error_type=type(exc).__name__)
        return []


def build_mcp_servers(
    *,
    static_config_path: Path | None,
    server_name: str,
    server_url: str | None,
    turn_token: str | None,
) -> dict | str | None:
    """Resolve the `mcp_servers` value handed to the Claude Agent SDK.

    Without a per-turn scoped token (or a configured server url) this preserves
    the legacy behaviour byte-for-byte: the static `mcp.json` path passthrough,
    or `None` when unset. With both present it merges the static servers (if any)
    with a per-turn streamable-HTTP entry carrying the token as a bearer; the
    per-turn entry wins on a name collision.
    """
    if server_url is None or turn_token is None:
        return str(static_config_path) if static_config_path else None

    servers: dict[str, Any] = {}
    if static_config_path is not None:
        try:
            parsed = json.loads(static_config_path.read_text(encoding="utf-8"))
            servers = dict(parsed.get("mcpServers") or {})
        except Exception as exc:  # noqa: BLE001
            log.warning("mcp.static_config_unreadable", error_type=type(exc).__name__)
            servers = {}

    servers[server_name] = {
        "type": "http",
        "url": server_url,
        "headers": {"Authorization": f"Bearer {turn_token}"},
    }
    return servers


@contextmanager
def private_mcp_config(servers: dict[str, Any]) -> Iterator[Path]:
    """Write `servers` as an mcp.json readable only by this user; delete it on exit.

    The SDK serialises a dict `mcp_servers` into `--mcp-config '<json>'` on the CLI's
    argv, where the per-turn bearer is visible to any process via `ps`. Handing the
    SDK a file path keeps it off argv. The file lives in a fresh 0700 directory
    outside the turn workspace, so the agent's cwd-scoped tools do not list it.
    """
    directory = Path(tempfile.mkdtemp(prefix="claude-sidecar-mcp-"))
    path = directory / "mcp.json"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": servers}, f)
        yield path
    finally:
        shutil.rmtree(directory, ignore_errors=True)
