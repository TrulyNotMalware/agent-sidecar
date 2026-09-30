"""What the CLI process can see: the turn token and the sidecar's own secrets."""

import json
from pathlib import Path

import pytest

from .harness import converse, precondition, wait_until

pytestmark = pytest.mark.integration

TOKEN = "turn-token-9f8e7d6c"


def _fake_messages(srv) -> list[str]:
    return [msg for _pid, msg in srv.fake_log_lines()]


def test_turn_token_reaches_the_cli_only_through_a_private_file(start_sidecar):
    srv = start_sidecar(
        mode="normal", MCP_SERVER_URL="http://127.0.0.1:9/mcp", MCP_SERVER_NAME="tools"
    )

    r = converse(srv.port, "k-token", headers={"X-Turn-Token": TOKEN})
    precondition(r.event_names == ["session", "text", "done"], f"turn failed: {r.events}")

    messages = _fake_messages(srv)
    [argv_line] = [m for m in messages if m.startswith("start")]
    assert TOKEN not in argv_line  # argv is world-readable via ps
    [config_line] = [m for m in messages if m.startswith("mcp_config_file=")]
    config = json.loads(config_line.removeprefix("mcp_config_file="))
    assert f"Bearer {TOKEN}" in config["content"]  # ...but the CLI still gets it
    assert wait_until(lambda: not Path(config["path"]).exists(), timeout=5)


def test_cli_environment_does_not_carry_sidecar_secrets(start_sidecar):
    srv = start_sidecar(mode="normal", OPENAI_API_KEY="sk-openai-for-codex-only")

    r = converse(srv.port, "k-env")
    precondition(r.event_names == ["session", "text", "done"], f"turn failed: {r.events}")

    messages = _fake_messages(srv)
    assert "env BEARER_SECRET=unset" in messages
    assert "env OPENAI_API_KEY=unset" in messages
