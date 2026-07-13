import json
from pathlib import Path
from typing import Any

from .observability.logging import get_logger

log = get_logger("sidecar.mcp")


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
