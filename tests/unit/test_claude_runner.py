from pathlib import Path

import pytest

from sidecar import claude_runner
from sidecar.errors import ApiError, ErrorCode


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
        system = options.system_prompt
        if isinstance(system, dict) and Path(system["path"]).is_file():
            seen["system_file_content"] = Path(system["path"]).read_text()
            seen["system_file_mode"] = Path(system["path"]).stat().st_mode & 0o777
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


def _install_failing_query(monkeypatch, exc: Exception, *messages, stderr=()) -> None:
    import claude_agent_sdk

    async def fake_query(*, prompt, options):
        for line in stderr:
            options.stderr(line)  # what the SDK does with each line the CLI writes
        for m in messages:
            yield m
        raise exc

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)


def _error_result(**fields):
    from claude_agent_sdk import ResultMessage

    return ResultMessage(
        subtype=fields.pop("subtype", "success"),
        duration_ms=1,
        duration_api_ms=1,
        is_error=True,
        num_turns=1,
        session_id="s",
        **fields,
    )


async def test_error_result_raised_by_the_sdk_is_the_clients_to_see(monkeypatch):
    from claude_agent_sdk import ResultError

    data = {"subtype": "success", "result": "Prompt is too long", "is_error": True}
    _install_failing_query(
        monkeypatch, ResultError("Claude Code returned an error result: …", data, exit_code=1)
    )

    with pytest.raises(ApiError) as exc_info:
        await _run()

    assert exc_info.value.code is ErrorCode.SDK_ERROR
    assert exc_info.value.message == "Prompt is too long"
    assert exc_info.value.detail is None


async def test_error_result_message_wins_over_the_sdk_restating_it(monkeypatch):
    from claude_agent_sdk import ResultError

    result = _error_result(subtype="error_max_turns", errors=["Reached max turns (3)"])
    _install_failing_query(monkeypatch, ResultError("restated", {}, exit_code=1), result)

    with pytest.raises(ApiError) as exc_info:
        await _run()

    assert exc_info.value.message == "Reached max turns (3)"


async def test_cli_failure_keeps_stderr_and_output_off_the_wire(monkeypatch):
    from claude_agent_sdk import CLIJSONDecodeError, ProcessError

    _install_failing_query(
        monkeypatch,
        ProcessError("Command failed", 2, "Check stderr output for details"),
        stderr=["starting", "fatal: cannot reach the API"],
    )
    with pytest.raises(ApiError) as exc_info:
        await _run()
    assert exc_info.value.message == "claude CLI failed (ProcessError, exit code 2)"
    assert exc_info.value.detail.endswith("stderr: starting\nfatal: cannot reach the API")

    bad_line = '{"type":"assistant","text":"the user\'s private words'
    _install_failing_query(monkeypatch, CLIJSONDecodeError(bad_line, ValueError("bad json")))
    with pytest.raises(ApiError) as exc_info:
        await _run()
    assert exc_info.value.message == "claude CLI failed (CLIJSONDecodeError)"
    assert "private words" not in exc_info.value.detail  # conversation content


async def test_system_prompt_goes_to_a_private_file_not_argv(monkeypatch):
    seen = _install_fake_query(monkeypatch)

    await _run(system_prompt="Answer tersely.")

    system = seen["options"].system_prompt
    assert system["type"] == "file"  # --system-prompt-file, not --system-prompt <text>
    assert seen["system_file_content"] == "Answer tersely."
    assert seen["system_file_mode"] == 0o600
    assert not Path(system["path"]).exists()  # removed once the turn ends


async def test_settings_api_key_reaches_the_cli(monkeypatch):
    # Settings may read it from .env, which the CLI's inherited environment lacks.
    seen = _install_fake_query(monkeypatch)

    await _run(anthropic_api_key="sk-ant-from-dotenv")

    assert seen["options"].env["ANTHROPIC_API_KEY"] == "sk-ant-from-dotenv"


async def test_api_error_prose_is_not_streamed_as_text(monkeypatch):
    # The CLI reports an API failure as a synthetic assistant message, then an error
    # result: only the (scrubbed) terminal frame should carry it.
    from claude_agent_sdk import AssistantMessage, ResultError, TextBlock

    synthetic = AssistantMessage(
        content=[TextBlock(text="API Error: 401 invalid x-api-key")],
        model="x",
        error="authentication_failed",
    )
    data = {"subtype": "success", "result": "API Error: 401 invalid x-api-key"}
    _install_failing_query(monkeypatch, ResultError("…", data, exit_code=1), synthetic)

    events = []
    with pytest.raises(ApiError) as exc_info:
        async for ev in claude_runner.run_turn(
            prompt="hi",
            cwd=Path("/tmp"),
            system_prompt=None,
            resume_session_id=None,
            mcp_config_path=None,
        ):
            events.append(ev)

    assert events == []
    assert exc_info.value.message == "API Error: 401 invalid x-api-key"


async def test_stderr_lines_are_capped_in_length_and_number(monkeypatch):
    from claude_agent_sdk import ProcessError

    logged: list[tuple[str, dict]] = []

    class Recorder:
        def info(self, event, **kw):
            logged.append((event, kw))

        warning = info

    monkeypatch.setattr(claude_runner, "log", Recorder())
    cap, count = claude_runner._STDERR_LINE_CHARS, claude_runner._STDERR_LOG_LINES
    _install_failing_query(
        monkeypatch, ProcessError("Command failed", 1, None), stderr=["x" * 50_000] * (count + 5)
    )

    with pytest.raises(ApiError):
        await _run()

    lines = [kw["line"] for event, kw in logged if event == "claude.stderr"]
    assert len(lines) == count
    assert {len(line) for line in lines} == {cap}
    assert [event for event, _ in logged].count("claude.stderr_not_logged") == 1
