import pytest
from pydantic import ValidationError

from sidecar.config import Settings


def test_missing_bearer_secret_fails_at_startup(monkeypatch):
    monkeypatch.delenv("BEARER_SECRET", raising=False)

    with pytest.raises(ValidationError, match="bearer_secret"):
        Settings(_env_file=None)


def test_empty_bearer_secret_fails_at_startup(monkeypatch):
    monkeypatch.setenv("BEARER_SECRET", "")

    with pytest.raises(ValidationError, match="bearer_secret"):
        Settings(_env_file=None)


def test_bearer_secret_is_not_exposed_in_repr():
    s = Settings(_env_file=None, bearer_secret="super-secret-value")

    assert "super-secret-value" not in repr(s)
    assert s.bearer_secret.get_secret_value() == "super-secret-value"


def test_codex_defaults_to_read_only_sandbox_and_parses_passthrough():
    s = Settings(_env_file=None, bearer_secret="x", codex_env_passthrough=" AZURE_KEY, ,FOO ")

    assert s.codex_sandbox == "read-only"
    assert s.codex_env_passthrough_names == ("AZURE_KEY", "FOO")


def test_codex_sandbox_rejects_unknown_modes():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, bearer_secret="x", codex_sandbox="yolo")


def test_shutdown_grace_must_leave_room_for_a_frame():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, bearer_secret="x", shutdown_grace_sec=0)


def test_bearer_secret_trailing_newline_is_stripped():
    s = Settings(_env_file=None, bearer_secret="from-a-k8s-secret\n")
    assert s.bearer_secret.get_secret_value() == "from-a-k8s-secret"


def test_whitespace_only_bearer_secret_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, bearer_secret=" \n ")


def test_anthropic_api_key_is_not_exposed_in_repr():
    s = Settings(_env_file=None, bearer_secret="x", anthropic_api_key="sk-ant-secret")
    assert "sk-ant-secret" not in repr(s)
