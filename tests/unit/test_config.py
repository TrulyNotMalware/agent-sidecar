import pytest
from pydantic import ValidationError

from sidecar.config import Settings


def _settings(**overrides: object) -> Settings:
    # Raw env-style values (plain str for a SecretStr, unknown literals) are what these
    # tests feed through pydantic-settings' validation, so they are untyped on purpose.
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]  # raw values under test


def test_missing_bearer_secret_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BEARER_SECRET", raising=False)

    with pytest.raises(ValidationError, match="bearer_secret"):
        Settings(_env_file=None)


def test_empty_bearer_secret_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEARER_SECRET", "")

    with pytest.raises(ValidationError, match="bearer_secret"):
        Settings(_env_file=None)


def test_bearer_secret_is_not_exposed_in_repr() -> None:
    s = _settings(bearer_secret="super-secret-value")

    assert "super-secret-value" not in repr(s)
    assert s.bearer_secret.get_secret_value() == "super-secret-value"


def test_codex_defaults_to_read_only_sandbox_and_parses_passthrough() -> None:
    s = _settings(bearer_secret="x", codex_env_passthrough=" AZURE_KEY, ,FOO ")

    assert s.codex_sandbox == "read-only"
    assert s.codex_env_passthrough_names == ("AZURE_KEY", "FOO")


def test_codex_sandbox_rejects_unknown_modes() -> None:
    with pytest.raises(ValidationError):
        _settings(bearer_secret="x", codex_sandbox="yolo")


def test_shutdown_grace_must_leave_room_for_a_frame() -> None:
    with pytest.raises(ValidationError):
        _settings(bearer_secret="x", shutdown_grace_sec=0)


def test_bearer_secret_trailing_newline_is_stripped() -> None:
    s = _settings(bearer_secret="from-a-k8s-secret\n")
    assert s.bearer_secret.get_secret_value() == "from-a-k8s-secret"


def test_whitespace_only_bearer_secret_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(bearer_secret=" \n ")


def test_anthropic_api_key_is_not_exposed_in_repr() -> None:
    s = _settings(bearer_secret="x", anthropic_api_key="sk-ant-secret")
    assert "sk-ant-secret" not in repr(s)


def test_allowed_tool_rules_keep_commas_inside_parentheses() -> None:
    s = _settings(
        bearer_secret="x",
        claude_allowed_tools="Bash(git log --format=a,b:*), Read ,WebFetch",
        claude_disallowed_tools="mcp__domain-tools__delete_all",
    )

    assert s.claude_allowed_tools_names == ("Bash(git log --format=a,b:*)", "Read", "WebFetch")
    assert s.claude_disallowed_tools_names == ("mcp__domain-tools__delete_all",)


def test_unknown_setting_source_fails_at_startup() -> None:
    with pytest.raises(ValidationError, match="bogus"):
        _settings(bearer_secret="x", claude_setting_sources="user,bogus")


@pytest.mark.parametrize("given", ["info", "Info", "INFO"])
def test_log_level_is_case_insensitive(given: str) -> None:
    assert _settings(bearer_secret="x", log_level=given).log_level == "INFO"


def test_unknown_log_level_fails_at_startup() -> None:
    with pytest.raises(ValidationError):
        _settings(bearer_secret="x", log_level="verbose")


def test_mcp_server_name_default_is_neutral_and_validated() -> None:
    assert _settings(bearer_secret="x").mcp_server_name == "domain-tools"
    with pytest.raises(ValidationError):
        _settings(bearer_secret="x", mcp_server_name="a.b")  # TOML key path


def test_anthropic_workspace_id_is_stripped_and_blank_means_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)

    def workspace_id(**given: object) -> str | None:
        return _settings(bearer_secret="x", **given).anthropic_workspace_id

    assert workspace_id() is None
    assert workspace_id(anthropic_workspace_id=" wrkspc_01Ab-c\n") == "wrkspc_01Ab-c"
    assert workspace_id(anthropic_workspace_id=" ") is None  # a copied .env.example


@pytest.mark.parametrize(
    "given", ["wrkspc_a\nanthropic-beta: x", "wrkspc_a: x", "wrkspc a", "wrkspc_a\r\nx"]
)
def test_anthropic_workspace_id_cannot_add_a_header_line(given: str) -> None:
    # It becomes an ANTHROPIC_CUSTOM_HEADERS line: newline / colon would inject headers.
    with pytest.raises(ValidationError, match="anthropic_workspace_id"):
        _settings(bearer_secret="x", anthropic_workspace_id=given)
