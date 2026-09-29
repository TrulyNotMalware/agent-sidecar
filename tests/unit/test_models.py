import pytest
from pydantic import ValidationError

from sidecar.models import ConverseRequest


def test_minimum_request():
    req = ConverseRequest.model_validate({"sessionKey": "k", "prompt": "hi"})
    assert req.session_key == "k"
    assert req.prompt == "hi"
    assert req.mode == "session"


def test_camelcase_aliases_round_trip():
    payload = {
        "sessionKey": "k",
        "prompt": "hi",
        "sessionId": "sess_1",
        "systemPrompt": "be terse",
        "appendSystemPrompt": None,
        "mode": "stateless",
    }
    req = ConverseRequest.model_validate(payload)
    assert req.session_id == "sess_1"
    assert req.system_prompt == "be terse"
    assert req.mode == "stateless"


def test_extra_fields_rejected():
    with pytest.raises(ValidationError):
        ConverseRequest.model_validate({"sessionKey": "k", "prompt": "hi", "junk": 1})


def test_empty_prompt_rejected():
    with pytest.raises(ValidationError):
        ConverseRequest.model_validate({"sessionKey": "k", "prompt": ""})


def test_empty_session_key_rejected():
    with pytest.raises(ValidationError):
        ConverseRequest.model_validate({"sessionKey": "", "prompt": "hi"})


@pytest.mark.parametrize(
    "session_id",
    ["01a0ec1c-14d2-7d12-aaa7-47100d58f161", "sess_fake-1", "a"],
)
def test_session_id_accepts_real_ids(session_id):
    req = ConverseRequest.model_validate(
        {"sessionKey": "k", "prompt": "hi", "sessionId": session_id}
    )
    assert req.session_id == session_id


@pytest.mark.parametrize(
    "session_id",
    [
        "--last",  # codex: resume another sessionKey's most recent thread
        "-csandbox_mode=danger-full-access",
        "--dangerously-skip-permissions",
        "",
        "has space",
        "x" * 129,
    ],
)
def test_session_id_rejects_values_a_cli_could_parse_as_flags(session_id):
    with pytest.raises(ValidationError):
        ConverseRequest.model_validate(
            {"sessionKey": "k", "prompt": "hi", "sessionId": session_id}
        )
