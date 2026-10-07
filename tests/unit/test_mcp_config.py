import json

from sidecar.mcp import per_turn_mcp_servers, static_mcp_servers

PER_TURN = {
    "type": "http",
    "url": "https://app.example/mcp",
    "headers": {"Authorization": "Bearer tok-abc"},
}


def _merged(static: dict, *, server_url: str | None, turn_token: str | None) -> dict | None:
    return per_turn_mcp_servers(
        static, server_name="domain-tools", server_url=server_url, turn_token=turn_token
    )


def test_url_unset_means_nothing_to_merge(tmp_path):
    static = tmp_path / "mcp.json"
    static.write_text('{"mcpServers": {"other": {}}}')

    # The runner then passes the static mcp.json to the CLI as a path, unchanged.
    assert _merged(static_mcp_servers(static), server_url=None, turn_token="tok-1") is None


def test_all_unset_means_nothing_to_merge():
    assert _merged(static_mcp_servers(None), server_url=None, turn_token=None) is None


def test_url_and_token_without_static_builds_bearer_entry():
    result = _merged({}, server_url="https://app.example/mcp", turn_token="tok-abc")

    assert result == {"domain-tools": PER_TURN}


def test_static_file_merge_keeps_other_servers(tmp_path):
    static = tmp_path / "mcp.json"
    static.write_text(
        json.dumps({"mcpServers": {"other": {"type": "sse", "url": "http://other/mcp"}}})
    )

    result = _merged(
        static_mcp_servers(static), server_url="https://app.example/mcp", turn_token="tok-abc"
    )

    assert result == {"other": {"type": "sse", "url": "http://other/mcp"}, "domain-tools": PER_TURN}


def test_name_collision_prefers_per_turn_entry(tmp_path):
    static = tmp_path / "mcp.json"
    static.write_text(
        json.dumps({"mcpServers": {"domain-tools": {"type": "sse", "url": "http://stale/mcp"}}})
    )
    servers = static_mcp_servers(static)

    result = _merged(servers, server_url="https://app.example/mcp", turn_token="tok-abc")

    assert result == {"domain-tools": PER_TURN}
    assert servers["domain-tools"]["url"] == "http://stale/mcp"  # the input is not mutated


def test_unparseable_static_file_still_returns_per_turn_entry(tmp_path):
    static = tmp_path / "mcp.json"
    static.write_text("{ not valid json")

    assert static_mcp_servers(static) == {}
    result = _merged(
        static_mcp_servers(static), server_url="https://app.example/mcp", turn_token="tok-abc"
    )

    assert result == {"domain-tools": PER_TURN}


def test_private_mcp_config_is_owner_only_and_removed_on_exit():
    from sidecar.mcp import private_mcp_config

    servers = {"domain-tools": {"type": "http", "url": "u", "headers": {"Authorization": "t"}}}
    with private_mcp_config(servers) as path:
        assert json.loads(path.read_text()) == {"mcpServers": servers}
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    assert not path.exists()
    assert not path.parent.exists()


def test_private_mcp_config_is_removed_when_the_turn_fails():
    import pytest

    from sidecar.mcp import private_mcp_config

    with pytest.raises(RuntimeError), private_mcp_config({"s": {}}) as path:
        raise RuntimeError("turn failed")
    assert not path.parent.exists()


def test_tool_prefix_uses_the_cli_normalization():
    from sidecar.mcp import mcp_tool_prefix

    assert mcp_tool_prefix("domain-tools") == "mcp__domain-tools"
    assert mcp_tool_prefix("v1.2") == "mcp__v1_2"  # the CLI names its tools mcp__v1_2__*
    assert mcp_tool_prefix("x,Bash") == "mcp__x_Bash"  # cannot split --allowedTools
    assert mcp_tool_prefix("has space") == "mcp__has_space"


def test_static_server_names_tolerate_bad_files(tmp_path):
    from sidecar.mcp import static_mcp_server_names

    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{ nope")
    not_a_dict = tmp_path / "list.json"
    not_a_dict.write_text(json.dumps({"mcpServers": ["a", "b"]}))

    assert static_mcp_server_names(None) == []
    assert static_mcp_server_names(tmp_path / "missing.json") == []
    assert static_mcp_server_names(bad_json) == []
    assert static_mcp_server_names(not_a_dict) == []
