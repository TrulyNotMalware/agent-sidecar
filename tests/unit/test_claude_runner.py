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
        "timeout_sec": 5,
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
