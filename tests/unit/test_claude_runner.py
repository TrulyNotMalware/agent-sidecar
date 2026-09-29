from pathlib import Path

from sidecar import claude_runner


def _install_fake_query(monkeypatch) -> dict:
    """Replace claude_agent_sdk.query; record the options the runner built."""
    import claude_agent_sdk

    seen: dict = {}

    async def fake_query(*, prompt, options):
        seen["options"] = options
        mcp = options.mcp_servers
        if isinstance(mcp, str) and Path(mcp).is_file():
            seen["mcp_file_content"] = Path(mcp).read_text()
            seen["mcp_file_mode"] = Path(mcp).stat().st_mode & 0o777
        return
        yield  # unreachable; makes this an async generator like the real query()

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    return seen


async def _run(**overrides) -> list:
    kwargs = {
        "prompt": "hi",
        "cwd": Path("/tmp"),
        "system_prompt": None,
        "resume_session_id": None,
        "mcp_config_path": None,
    }
    kwargs.update(overrides)
    return [ev async for ev in claude_runner.run_turn(**kwargs)]


async def test_turn_token_goes_to_a_private_file_not_inline_json(monkeypatch):
    seen = _install_fake_query(monkeypatch)

    await _run(
        mcp_server_url="https://app.example/mcp",
        mcp_server_name="tools",
        turn_token="tok-secret",
    )

    mcp = seen["options"].mcp_servers
    # A str is passed to the CLI as a path; a dict would be inlined on argv.
    assert isinstance(mcp, str)
    assert "tok-secret" not in mcp
    assert "Bearer tok-secret" in seen["mcp_file_content"]
    assert seen["mcp_file_mode"] == 0o600
    assert not Path(mcp).exists()  # removed once the turn ends
    assert seen["options"].allowed_tools == ["mcp__tools"]


async def test_static_config_path_is_passed_through_unchanged(monkeypatch, tmp_path):
    static = tmp_path / "mcp.json"
    static.write_text('{"mcpServers": {}}')
    seen = _install_fake_query(monkeypatch)

    await _run(mcp_config_path=static)

    assert seen["options"].mcp_servers == str(static)
    assert static.exists()


async def test_sidecar_secrets_are_blanked_in_the_cli_env(monkeypatch):
    seen = _install_fake_query(monkeypatch)

    await _run()

    assert seen["options"].env["BEARER_SECRET"] == ""
    assert seen["options"].env["OPENAI_API_KEY"] == ""


async def test_extra_withheld_names_are_blanked_too(monkeypatch):
    seen = _install_fake_query(monkeypatch)

    await _run(withheld_env=("AZURE_OPENAI_KEY",))

    assert seen["options"].env["AZURE_OPENAI_KEY"] == ""
    assert seen["options"].env["BEARER_SECRET"] == ""


async def test_default_policy_is_explicit_and_hermetic(monkeypatch):
    seen = _install_fake_query(monkeypatch)

    await _run()

    options = seen["options"]
    assert options.permission_mode == "dontAsk"
    assert options.setting_sources == []  # no ~/.claude or project settings/hooks/plugins
    assert "strict-mcp-config" in options.extra_args  # no MCP servers from elsewhere
    assert options.tools is None  # CLI default built-in toolset (operator choice)


async def test_configured_mcp_servers_are_pre_approved(monkeypatch, tmp_path):
    static = tmp_path / "mcp.json"
    static.write_text('{"mcpServers": {"domain-tools": {}, "search.v2": {}, "tools": {}}}')
    seen = _install_fake_query(monkeypatch)

    await _run(
        mcp_config_path=static,
        mcp_server_url="https://app.example/mcp",
        mcp_server_name="tools",
        turn_token="tok",
        allowed_tools=("WebFetch",),
    )

    assert seen["options"].allowed_tools == [
        "WebFetch",
        "mcp__domain-tools",
        "mcp__search_v2",  # normalized like the CLI's tool names
        "mcp__tools",  # static and per-turn server share a name: listed once
    ]


async def test_tools_can_be_restricted_or_disabled(monkeypatch):
    seen = _install_fake_query(monkeypatch)

    await _run(tools=[])
    assert seen["options"].tools == []

    await _run(tools=["Read", "Grep"], permission_mode="default", setting_sources=("project",))
    assert seen["options"].tools == ["Read", "Grep"]
    assert seen["options"].permission_mode == "default"
    assert seen["options"].setting_sources == ["project"]


async def test_disallowed_tools_are_passed_through(monkeypatch):
    seen = _install_fake_query(monkeypatch)

    await _run(disallowed_tools=("mcp__domain-tools__delete_all", "WebFetch"))

    assert seen["options"].disallowed_tools == ["mcp__domain-tools__delete_all", "WebFetch"]


async def test_restricted_mode_is_opt_in(monkeypatch):
    seen = _install_fake_query(monkeypatch)

    await _run()
    assert "restricted" not in seen["options"].extra_args

    await _run(restricted=True)
    assert "restricted" in seen["options"].extra_args
