import logging

import pytest

from sidecar.errors import provider_message
from sidecar.observability import redaction
from sidecar.observability.logging import _scrub_processor
from sidecar.observability.redaction import (
    REDACTED,
    register_secrets,
    register_turn_secret,
    scrub_secrets,
)

R = REDACTED


def test_registered_values_are_replaced_longest_first(monkeypatch):
    monkeypatch.setattr(redaction, "_known", set())
    register_secrets("bearer-secret-value", "bearer-secret-value-2", None, "short")

    text = scrub_secrets("a bearer-secret-value-2 b bearer-secret-value c short")

    assert text == f"a {REDACTED} b {REDACTED} c short"  # too short to register


@pytest.mark.parametrize(
    ("text", "scrubbed"),
    [
        ("key sk-ant-api03-AbCdEf_123-xyz used", f"key sk-{R} used"),
        ('"error\\nsk-ant-api03-AbCdEf123xyz"', f'"error\\nsk-{R}"'),  # JSON-escaped
        ("API key: sk-proj-****abcd", f"API key: sk-{R}"),  # masked, as echoed back
        ("legacy sk-" + "a" * 48, f"legacy sk-{R}"),
        ("auth: Bearer eyJhbGciOi.x.y", f"auth: Bearer {R}"),
        ("Authorization: Basic dXNlcjpwYXNzd29yZA==", f"Authorization: Basic {R}"),
        ("x-api-key: gw-token-0123456789", f"x-api-key: {R}"),
        ("GET /v1?api_key=abcdef123456&x=1", f"GET /v1?api_key={R}&x=1"),
        ("hdr eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.sig", f"hdr {R}"),
        ("https://user:hunter22@gw.example/v1", f"https://{R}@gw.example/v1"),
    ],
)
def test_credential_shaped_strings_are_replaced(text, scrubbed):
    assert scrub_secrets(text) == scrubbed


@pytest.mark.parametrize(
    "text",
    [
        "Missing bearer authentication in header",
        "sessionKey 'tenant:sk-dashboard' has an in-flight turn",
        "task-management-dashboard uses sk-learn",
        "The token count exceeded max_tokens=4096",
    ],
)
def test_ordinary_text_is_left_alone(text):
    assert scrub_secrets(text) == text


def test_a_turn_secret_is_scrubbed_only_in_its_own_context():
    import contextvars

    def in_turn():
        register_turn_secret("per-turn-token-value")
        return scrub_secrets("mcp said: per-turn-token-value")

    assert contextvars.copy_context().run(in_turn) == f"mcp said: {R}"
    assert scrub_secrets("per-turn-token-value") == "per-turn-token-value"


def test_provider_message_is_scrubbed_and_bounded():
    message = provider_message("  quota for sk-proj-abcdef123456 " + "x" * 1000)

    assert "abcdef123456" not in message
    assert len(message) == 500
    assert message.endswith("…")


def test_every_logged_value_is_scrubbed_nested_too(monkeypatch):
    monkeypatch.setattr(redaction, "_known", {"the-bearer-secret"})

    out = _scrub_processor(None, "info", {
        "event": "turn.closed",
        "error_detail": "stderr: Authorization: Bearer the-bearer-secret",
        "nested": {"list": ["sk-proj-abcdef123456"]},
        "count": 3,
    })

    assert out["error_detail"] == f"stderr: Authorization: Bearer {REDACTED}"
    assert out["nested"] == {"list": [f"sk-{REDACTED}"]}
    assert out["count"] == 3


def test_plain_logging_is_scrubbed_too(caplog):
    # uvicorn and the Agent SDK log through `logging`, not structlog. (The app's
    # logging is configured when tests/conftest.py imports sidecar.app.)
    with caplog.at_level(logging.INFO):
        logging.getLogger("uvicorn.error").warning("upstream said %s", "Bearer abc.def.123456")
        try:
            raise RuntimeError("key sk-proj-abcdef123456")
        except RuntimeError:
            logging.getLogger("some.lib").exception("failed")

    first, second = caplog.records
    assert first.getMessage() == f"upstream said Bearer {R}"
    assert "abcdef123456" not in second.exc_text


def test_sdk_reader_errors_quoting_cli_output_are_dropped(caplog):
    with caplog.at_level(logging.INFO):
        logging.getLogger("claude_agent_sdk._internal.query").error(
            'Fatal error in message reader: Failed to decode JSON: {"text":"private words'
        )
        logging.getLogger("claude_agent_sdk._internal.query").warning("other SDK warning")

    assert [r.getMessage() for r in caplog.records] == ["other SDK warning"]
