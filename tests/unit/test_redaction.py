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


def test_registered_values_are_replaced_longest_first(monkeypatch: pytest.MonkeyPatch) -> None:
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
        ("Authorization: Basic dXNlcjpwYXNz", f"Authorization: Basic {R}"),  # user:pass
        ("Bearer ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef", f"Bearer {R}"),  # letters only, long
        ('"x\\nsk-ant-api03-AbCdEf123"', f'"x\\nsk-{R}"'),
        ("q=a%3Dsk-proj-abcdef123", f"q=a%3Dsk-{R}"),
        ("x-api-key: gw-token-0123456789", f"x-api-key: {R}"),
        ("GET /v1?api_key=abcdef123456&x=1", f"GET /v1?api_key={R}&x=1"),
        ("hdr eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.sig", f"hdr {R}"),
        ("https://user:hunter22@gw.example/v1", f"https://{R}@gw.example/v1"),
    ],
)
def test_credential_shaped_strings_are_replaced(text: str, scrubbed: str) -> None:
    assert scrub_secrets(text) == scrubbed


@pytest.mark.parametrize(
    "text",
    [
        "Missing bearer authentication in header",
        "sessionKey 'tenant:sk-dashboard' has an in-flight turn",
        "task-management-dashboard uses sk-learn",
        "The token count exceeded max_tokens=4096",
        "desk-admin-panel and mcp__domain-tools__task-admin-list",
        "the basic pay-as-you-go plan, basic rate-limit",
        "token: expired-or-invalid; secret: my-app-secret-name",
        "Basic Authentication required",
    ],
)
def test_ordinary_text_is_left_alone(text: str) -> None:
    assert scrub_secrets(text) == text


def test_a_turn_secret_is_scrubbed_only_in_its_own_context() -> None:
    import contextvars

    def in_turn() -> str:
        register_turn_secret("per-turn-token-value")
        return scrub_secrets("mcp said: per-turn-token-value")

    assert contextvars.copy_context().run(in_turn) == f"mcp said: {R}"
    assert scrub_secrets("per-turn-token-value") == "per-turn-token-value"


def test_provider_message_is_scrubbed_and_bounded() -> None:
    message = provider_message("  quota for sk-proj-abcdef123456 " + "x" * 1000)

    assert "abcdef123456" not in message
    assert len(message) == 500
    assert message.endswith("…")


def test_every_logged_value_is_scrubbed_nested_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(redaction, "_known", {"the-bearer-secret"})

    out = _scrub_processor(
        None,
        "info",
        {
            "event": "turn.closed",
            "error_detail": "stderr: Authorization: Bearer the-bearer-secret",
            "nested": {"list": ["sk-proj-abcdef123456"]},
            "count": 3,
        },
    )

    assert out["error_detail"] == f"stderr: Authorization: Bearer {REDACTED}"
    assert out["nested"] == {"list": [f"sk-{REDACTED}"]}
    assert out["count"] == 3


def test_plain_logging_is_scrubbed_after_formatting(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        log = logging.getLogger("some.lib")
        log.info("Authorization: Bearer %s", "abc123def456ghi789")  # split across format/arg
        log.info("connecting to https://%s:%s@%s", "user", "pw123456", "host")
        log.error(RuntimeError("boom sk-ant-api03-AbCdEf123"))  # not a str message
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d', "1.2.3.4", "GET", "/x?api_key=abc123456789", "1.1", 200
        )

    messages = [r.getMessage() for r in caplog.records]
    assert messages[0] == f"Authorization: Bearer {R}"
    assert messages[1] == f"connecting to https://{R}@host"
    assert messages[2] == f"boom sk-{R}"
    args = caplog.records[3].args
    assert isinstance(args, tuple)
    assert args[2] == f"/x?api_key={R}"  # its formatter unpacks args


def test_plain_logging_is_scrubbed_too(caplog: pytest.LogCaptureFixture) -> None:
    # uvicorn and the Agent SDK log through `logging`, not structlog. (The app's
    # logging is configured once per test session by tests/conftest.py.)
    with caplog.at_level(logging.INFO):
        logging.getLogger("uvicorn.error").warning("upstream said %s", "Bearer abc.def.123456")
        try:
            raise RuntimeError("key sk-proj-abcdef123456")
        except RuntimeError:
            logging.getLogger("some.lib").exception("failed")

    first, second = caplog.records
    assert first.getMessage() == f"upstream said Bearer {R}"
    assert second.exc_text is not None
    assert "abcdef123456" not in second.exc_text


def test_sdk_reader_errors_quoting_cli_output_are_dropped(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("claude_agent_sdk._internal.query").error(
            'Fatal error in message reader: Failed to decode JSON: {"text":"private words'
        )
        logging.getLogger("claude_agent_sdk._internal.query").debug(
            "Skipping non-JSON line from CLI stdout: private words"
        )
        logging.getLogger("claude_agent_sdk._internal.query").warning("other SDK warning")

    assert [r.getMessage() for r in caplog.records] == ["other SDK warning"]
