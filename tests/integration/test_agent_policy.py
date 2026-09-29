"""The CLI flags the real SDK passes for the sidecar's agent policy."""

import json

import pytest

from .harness import converse, precondition

pytestmark = pytest.mark.integration


def _cli_argv(srv) -> list[str]:
    [line] = [msg for _pid, msg in srv.fake_log_lines() if msg.startswith("start")]
    return json.loads(line.split(" argv=", 1)[1])


def test_default_policy_reaches_the_cli(start_sidecar, tmp_path):
    static = tmp_path / "mcp.json"
    static.write_text(json.dumps({"mcpServers": {"domain-tools": {"type": "http", "url": "u"}}}))
    srv = start_sidecar(mode="normal", MCP_CONFIG_PATH=str(static))

    r = converse(srv.port, "k-policy")
    precondition(r.event_names == ["session", "text", "done"], f"turn failed: {r.events}")
    argv = _cli_argv(srv)

    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--setting-sources=" in argv  # load no user/project/local settings
    assert "--strict-mcp-config" in argv
    assert "mcp__domain-tools" in argv[argv.index("--allowedTools") + 1].split(",")
    assert "--tools" not in argv  # CLI default toolset unless CLAUDE_TOOLS is set


def test_claude_tools_empty_disables_built_ins(start_sidecar):
    srv = start_sidecar(mode="normal", CLAUDE_TOOLS="")

    r = converse(srv.port, "k-no-tools")
    precondition(r.event_names == ["session", "text", "done"], f"turn failed: {r.events}")
    argv = _cli_argv(srv)

    assert argv[argv.index("--tools") + 1] == ""


def test_claude_restricted_reaches_the_cli(start_sidecar):
    srv = start_sidecar(mode="normal", CLAUDE_RESTRICTED="true")

    r = converse(srv.port, "k-restricted")
    precondition(r.event_names == ["session", "text", "done"], f"turn failed: {r.events}")

    assert "--restricted" in _cli_argv(srv)
