from sidecar.observability.logging import REDACTED, _redact_processor


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
