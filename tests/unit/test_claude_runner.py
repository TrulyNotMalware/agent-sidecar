from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import ClaudeAgentOptions, Message, ResultMessage

from sidecar import claude_runner
from sidecar.claude_runner import ClaudePolicy
from sidecar.errors import ApiError, ErrorCode
from sidecar.events import RunnerEvent, TurnSpec


def _install_fake_query(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace claude_agent_sdk.query; record the options the runner built."""
    import claude_agent_sdk

    seen: dict[str, Any] = {}

    async def fake_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncGenerator[Message, None]:
        seen["options"] = options
        mcp = options.mcp_servers
        if isinstance(mcp, str) and Path(mcp).is_file():
            seen["mcp_file_content"] = Path(mcp).read_text()
            seen["mcp_file_mode"] = Path(mcp).stat().st_mode & 0o777
        system = options.system_prompt
        if isinstance(system, dict):
            system_file = Path(cast("dict[str, str]", system)["path"])
            if system_file.is_file():
                seen["system_file_content"] = system_file.read_text()
                seen["system_file_mode"] = system_file.stat().st_mode & 0o777
        return
        yield  # type: ignore[unreachable]  # makes this an async generator like the real query()

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    return seen


async def _run(
    spec: TurnSpec | None = None, *, policy: ClaudePolicy | None = None
) -> list[RunnerEvent]:
    spec = TurnSpec(prompt="hi") if spec is None else spec
    return [ev async for ev in claude_runner.run_turn(spec, cwd=Path("/tmp"), policy=policy)]


async def test_turn_token_goes_to_a_private_file_not_inline_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install_fake_query(monkeypatch)

    await _run(
        TurnSpec(
            prompt="hi",
            mcp_server_url="https://app.example/mcp",
            mcp_server_name="tools",
            turn_token="tok-secret",
        )
    )

    mcp = seen["options"].mcp_servers
    # A str is passed to the CLI as a path; a dict would be inlined on argv.
    assert isinstance(mcp, str)
    assert "tok-secret" not in mcp
    assert "Bearer tok-secret" in seen["mcp_file_content"]
    assert seen["mcp_file_mode"] == 0o600
    assert not Path(mcp).exists()  # removed once the turn ends
    assert seen["options"].allowed_tools == ["mcp__tools"]


async def test_static_config_path_is_passed_through_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    static = tmp_path / "mcp.json"
    static.write_text('{"mcpServers": {}}')
    seen = _install_fake_query(monkeypatch)

    await _run(TurnSpec(prompt="hi", mcp_config_path=static))

    assert seen["options"].mcp_servers == str(static)
    assert static.exists()


async def test_sidecar_secrets_are_blanked_in_the_cli_env(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install_fake_query(monkeypatch)

    await _run()

    assert seen["options"].env["BEARER_SECRET"] == ""
    assert seen["options"].env["OPENAI_API_KEY"] == ""


async def test_extra_withheld_names_are_blanked_too(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install_fake_query(monkeypatch)

    await _run(policy=ClaudePolicy(withheld_env=("AZURE_OPENAI_KEY",)))

    assert seen["options"].env["AZURE_OPENAI_KEY"] == ""
    assert seen["options"].env["BEARER_SECRET"] == ""


async def test_default_policy_is_explicit_and_hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install_fake_query(monkeypatch)

    await _run()

    options = seen["options"]
    assert options.permission_mode == "dontAsk"
    assert options.setting_sources == []  # no ~/.claude or project settings/hooks/plugins
    assert "strict-mcp-config" in options.extra_args  # no MCP servers from elsewhere
    assert options.tools is None  # CLI default built-in toolset (operator choice)


async def test_configured_mcp_servers_are_pre_approved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    static = tmp_path / "mcp.json"
    static.write_text('{"mcpServers": {"domain-tools": {}, "search.v2": {}, "tools": {}}}')
    seen = _install_fake_query(monkeypatch)

    await _run(
        TurnSpec(
            prompt="hi",
            mcp_config_path=static,
            mcp_server_url="https://app.example/mcp",
            mcp_server_name="tools",
            turn_token="tok",
        ),
        policy=ClaudePolicy(allowed_tools=("WebFetch",)),
    )

    assert seen["options"].allowed_tools == [
        "WebFetch",
        "mcp__domain-tools",
        "mcp__search_v2",  # normalized like the CLI's tool names
        "mcp__tools",  # static and per-turn server share a name: listed once
    ]


async def test_tools_can_be_restricted_or_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install_fake_query(monkeypatch)

    await _run(policy=ClaudePolicy(tools=()))
    assert seen["options"].tools == []

    await _run(
        policy=ClaudePolicy(
            tools=("Read", "Grep"), permission_mode="default", setting_sources=("project",)
        )
    )
    assert seen["options"].tools == ["Read", "Grep"]
    assert seen["options"].permission_mode == "default"
    assert seen["options"].setting_sources == ["project"]


async def test_disallowed_tools_are_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install_fake_query(monkeypatch)

    await _run(policy=ClaudePolicy(disallowed_tools=("mcp__domain-tools__delete_all", "WebFetch")))

    assert seen["options"].disallowed_tools == ["mcp__domain-tools__delete_all", "WebFetch"]


async def test_restricted_mode_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install_fake_query(monkeypatch)

    await _run()
    assert "restricted" not in seen["options"].extra_args

    await _run(policy=ClaudePolicy(restricted=True))
    assert "restricted" in seen["options"].extra_args


def _install_failing_query(
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    *messages: Message,
    stderr: Sequence[str] = (),
) -> None:
    import claude_agent_sdk

    async def fake_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncGenerator[Message, None]:
        for line in stderr:
            assert options.stderr is not None
            options.stderr(line)  # what the SDK does with each line the CLI writes
        for m in messages:
            yield m
        raise exc

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)


def _error_result(subtype: str = "success", errors: list[str] | None = None) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=True,
        num_turns=1,
        session_id="s",
        errors=errors,
    )


async def test_error_result_raised_by_the_sdk_is_the_clients_to_see(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


async def test_error_result_message_wins_over_the_sdk_restating_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_agent_sdk import ResultError

    result = _error_result(subtype="error_max_turns", errors=["Reached max turns (3)"])
    _install_failing_query(monkeypatch, ResultError("restated", {}, exit_code=1), result)

    with pytest.raises(ApiError) as exc_info:
        await _run()

    assert exc_info.value.message == "Reached max turns (3)"


async def test_cli_failure_keeps_stderr_and_output_off_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_agent_sdk import CLIJSONDecodeError, ProcessError

    _install_failing_query(
        monkeypatch,
        ProcessError("Command failed", 2, "Check stderr output for details"),
        stderr=["starting", "fatal: cannot reach the API"],
    )
    with pytest.raises(ApiError) as exc_info:
        await _run()
    assert exc_info.value.message == "claude CLI failed (ProcessError, exit code 2)"
    assert exc_info.value.detail is not None
    assert exc_info.value.detail.endswith("stderr: starting\nfatal: cannot reach the API")

    bad_line = '{"type":"assistant","text":"the user\'s private words'
    _install_failing_query(monkeypatch, CLIJSONDecodeError(bad_line, ValueError("bad json")))
    with pytest.raises(ApiError) as exc_info:
        await _run()
    assert exc_info.value.message == "claude CLI failed (CLIJSONDecodeError)"
    assert exc_info.value.detail is not None
    assert "private words" not in exc_info.value.detail  # conversation content


async def test_system_prompt_goes_to_a_private_file_not_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install_fake_query(monkeypatch)

    await _run(TurnSpec(prompt="hi", system_prompt="Answer tersely."))

    system = seen["options"].system_prompt
    assert system["type"] == "file"  # --system-prompt-file, not --system-prompt <text>
    assert seen["system_file_content"] == "Answer tersely."
    assert seen["system_file_mode"] == 0o600
    assert not Path(system["path"]).exists()  # removed once the turn ends


async def test_settings_api_key_reaches_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    # Settings may read it from .env, which the CLI's inherited environment lacks.
    seen = _install_fake_query(monkeypatch)

    await _run(policy=ClaudePolicy(anthropic_api_key="sk-ant-from-dotenv"))

    assert seen["options"].env["ANTHROPIC_API_KEY"] == "sk-ant-from-dotenv"


async def test_workspace_id_goes_out_as_a_custom_header(monkeypatch: pytest.MonkeyPatch) -> None:
    # With an API key the CLI never sends this header itself (an organization-scoped
    # key needs it); it does send ANTHROPIC_CUSTOM_HEADERS lines.
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    seen = _install_fake_query(monkeypatch)

    await _run(policy=ClaudePolicy(anthropic_workspace_id="wrkspc_01Ab"))

    assert seen["options"].env["ANTHROPIC_CUSTOM_HEADERS"] == "anthropic-workspace-id: wrkspc_01Ab"


async def test_workspace_header_is_merged_into_inherited_custom_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Setting the variable replaces the inherited one: the operator's headers must
    # survive, and a workspace line of theirs must not make a second one.
    monkeypatch.setenv(
        "ANTHROPIC_CUSTOM_HEADERS", "X-Gateway-Route: eu\r\nAnthropic-Workspace-Id: wrkspc_stale\n"
    )
    seen = _install_fake_query(monkeypatch)

    await _run(policy=ClaudePolicy(anthropic_workspace_id="wrkspc_01Ab"))

    lines = seen["options"].env["ANTHROPIC_CUSTOM_HEADERS"].split("\n")
    assert lines == ["X-Gateway-Route: eu", "anthropic-workspace-id: wrkspc_01Ab"]


async def test_workspace_header_merge_splits_lines_like_the_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The CLI splits on \n and \r\n only; str.splitlines() would also cut a value at
    # \x0c or \x85 and turn the rest into a line of its own.
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "X-Trace: a\x0cb\nX-Route: eu\x85x")
    seen = _install_fake_query(monkeypatch)

    await _run(policy=ClaudePolicy(anthropic_workspace_id="wrkspc_01Ab"))

    lines = seen["options"].env["ANTHROPIC_CUSTOM_HEADERS"].split("\n")
    assert lines == ["X-Trace: a\x0cb", "X-Route: eu\x85x", "anthropic-workspace-id: wrkspc_01Ab"]


async def test_withheld_custom_headers_are_not_brought_back_by_the_workspace_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "X-Codex-Gateway: secret")
    seen = _install_fake_query(monkeypatch)

    await _run(
        policy=ClaudePolicy(
            withheld_env=("ANTHROPIC_CUSTOM_HEADERS",), anthropic_workspace_id="wrkspc_01Ab"
        )
    )

    assert seen["options"].env["ANTHROPIC_CUSTOM_HEADERS"] == "anthropic-workspace-id: wrkspc_01Ab"


async def test_custom_headers_are_left_alone_without_a_workspace_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "X-Gateway-Route: eu")
    seen = _install_fake_query(monkeypatch)

    await _run()

    assert "ANTHROPIC_CUSTOM_HEADERS" not in seen["options"].env  # the CLI inherits it as is


async def test_api_error_prose_is_not_streamed_as_text(monkeypatch: pytest.MonkeyPatch) -> None:
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

    events: list[RunnerEvent] = []
    with pytest.raises(ApiError) as exc_info:
        async for ev in claude_runner.run_turn(TurnSpec(prompt="hi"), cwd=Path("/tmp")):
            events.append(ev)  # noqa: PERF401 — the raise lands mid-stream; what came before matters

    assert events == []
    assert exc_info.value.message == "API Error: 401 invalid x-api-key"


async def test_stderr_lines_are_capped_in_length_and_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_agent_sdk import ProcessError

    logged: list[tuple[str, dict[str, object]]] = []

    class Recorder:
        def info(self, event: str, **kw: object) -> None:
            logged.append((event, kw))

        warning = info

    monkeypatch.setattr(claude_runner, "log", Recorder())
    cap, count = claude_runner._STDERR_LINE_CHARS, claude_runner._STDERR_LOG_LINES
    _install_failing_query(
        monkeypatch, ProcessError("Command failed", 1, None), stderr=["x" * 50_000] * (count + 5)
    )

    with pytest.raises(ApiError):
        await _run()

    lines = [str(kw["line"]) for event, kw in logged if event == "claude.stderr"]
    assert len(lines) == count
    assert {len(line) for line in lines} == {cap}
    assert [event for event, _ in logged].count("claude.stderr_not_logged") == 1
