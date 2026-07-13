import json
from pathlib import Path

from sidecar.mcp import build_mcp_servers


def test_url_unset_passes_static_path_through_as_str():
    result = build_mcp_servers(
        static_config_path=Path("/etc/sidecar/mcp.json"),
        server_name="codecompanion",
        server_url=None,
        turn_token="tok-1",
    )

    assert result == "/etc/sidecar/mcp.json"


def test_all_unset_returns_none():
    result = build_mcp_servers(
        static_config_path=None,
        server_name="codecompanion",
        server_url=None,
        turn_token=None,
    )

    assert result is None


def test_url_and_token_without_static_builds_bearer_entry():
    result = build_mcp_servers(
        static_config_path=None,
        server_name="codecompanion",
        server_url="https://app.example/mcp",
        turn_token="tok-abc",
    )

    assert result == {
        "codecompanion": {
            "type": "http",
            "url": "https://app.example/mcp",
            "headers": {"Authorization": "Bearer tok-abc"},
        }
    }


def test_static_file_merge_keeps_other_servers(tmp_path):
    static = tmp_path / "mcp.json"
    static.write_text(
        json.dumps({"mcpServers": {"other": {"type": "sse", "url": "http://other/mcp"}}})
    )

    result = build_mcp_servers(
        static_config_path=static,
        server_name="codecompanion",
        server_url="https://app.example/mcp",
        turn_token="tok-abc",
    )

    assert result["other"] == {"type": "sse", "url": "http://other/mcp"}
    assert result["codecompanion"]["url"] == "https://app.example/mcp"


def test_name_collision_prefers_per_turn_entry(tmp_path):
    static = tmp_path / "mcp.json"
    static.write_text(
        json.dumps({"mcpServers": {"codecompanion": {"type": "sse", "url": "http://stale/mcp"}}})
    )

    result = build_mcp_servers(
        static_config_path=static,
        server_name="codecompanion",
        server_url="https://app.example/mcp",
        turn_token="tok-abc",
    )

    assert result["codecompanion"] == {
        "type": "http",
        "url": "https://app.example/mcp",
        "headers": {"Authorization": "Bearer tok-abc"},
    }


def test_unparseable_static_file_still_returns_per_turn_entry(tmp_path):
    static = tmp_path / "mcp.json"
    static.write_text("{ not valid json")

    result = build_mcp_servers(
        static_config_path=static,
        server_name="codecompanion",
        server_url="https://app.example/mcp",
        turn_token="tok-abc",
    )

    assert result == {
        "codecompanion": {
            "type": "http",
            "url": "https://app.example/mcp",
            "headers": {"Authorization": "Bearer tok-abc"},
        }
    }
