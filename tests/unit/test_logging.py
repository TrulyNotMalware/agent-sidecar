import io
import json
import logging

import structlog

from sidecar.observability.logging import REDACTED, _redact_processor, stdlib_formatter


def test_redact_processor_redacts_known_keys():
    event = {
        "event": "converse.start",
        "prompt": "secret content",
        "system_prompt": "secret",
        "delta": "hello",
        "session_key": "ok-to-keep",
    }
    out = _redact_processor(None, "info", event.copy())
    assert out["prompt"] == REDACTED
    assert out["system_prompt"] == REDACTED
    assert out["delta"] == REDACTED
    assert out["session_key"] == "ok-to-keep"


def test_redact_processor_keeps_empty_values():
    event = {"prompt": "", "delta": None, "session_key": "k"}
    out = _redact_processor(None, "info", event.copy())
    assert out["prompt"] == ""
    assert out["delta"] is None


def test_plain_logging_records_render_like_structlog_lines():
    # uvicorn's and the SDK's records: JSON with the bound context and a scrubbed traceback.
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(stdlib_formatter())
    logger = logging.getLogger("test.plain.json")
    logger.propagate = False
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    structlog.contextvars.bind_contextvars(turn_id="t-1")
    try:
        logger.info("Started server process [%d]", 7)
        try:
            raise ValueError("x-api-key: sk-proj-abcdef123456")
        except ValueError:
            logger.exception("Exception in ASGI application")
    finally:
        structlog.contextvars.clear_contextvars()
        logger.removeHandler(handler)

    first, second = (json.loads(line) for line in stream.getvalue().splitlines())
    assert first["event"] == "Started server process [7]"
    assert first["level"] == "info"
    assert first["turn_id"] == "t-1"
    assert first["timestamp"].endswith("Z")
    assert second["level"] == "error"
    assert "Traceback" in second["exception"]
    assert "abcdef123456" not in second["exception"]
