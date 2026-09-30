"""The codex runner against real processes: stdin prompt, process-group kill, resume binding."""

import json
import re

import pytest

from .harness import converse, pid_alive, precondition, wait_until

pytestmark = pytest.mark.integration

THREAD_ID = "01a0ec52-3a8c-7d43-871b-149bbb8c0acf"  # what fake_codex.py always reports


def _converse_resume(srv, session_key: str, session_id: str):
    import socket

    from .harness import BEARER

    body = json.dumps({"sessionKey": session_key, "prompt": "again", "sessionId": session_id})
    request = (
        f"POST /v1/converse HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {BEARER}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + body.encode()
    with socket.create_connection(("127.0.0.1", srv.port), timeout=10) as sock:
        sock.sendall(request)
        head = sock.recv(64)
    return int(head.split()[1])


def _codex_lines(srv) -> list[tuple[int, str]]:
    return [(pid, msg) for pid, msg in srv.fake_log_lines() if not msg.startswith("start mode")]


def test_prompt_reaches_codex_on_stdin_and_transient_errors_are_tolerated(start_sidecar):
    srv = start_sidecar(provider="codex")

    r = converse(srv.port, "k-codex", prompt="--last hello")

    # fake_codex emits a "Reconnecting... 1/5" error event before completing.
    assert r.event_names == ["session", "text", "done"]
    messages = [msg for _pid, msg in _codex_lines(srv)]
    [start] = [m for m in messages if m.startswith("start codex")]
    argv = json.loads(start.split("argv=", 1)[1])
    assert argv[-2:] == ["--", "-"]
    assert "--last hello" not in argv
    assert 'prompt="--last hello"' in messages


def test_timeout_kills_codex_and_the_rest_of_its_process_group(start_sidecar):
    srv = start_sidecar(mode="hang_with_child", provider="codex", TURN_TIMEOUT_SEC="2")

    r = converse(srv.port, "k-codex-timeout")

    assert r.terminal_events == ["error"]
    assert r.events[-1][1]["code"] == "timeout"
    messages = [msg for _pid, msg in _codex_lines(srv)]
    [codex_pid] = srv.cli_pids()
    [child] = [m for m in messages if m.startswith("child pid=")]
    child_pid = int(re.search(r"\d+", child).group())
    assert wait_until(lambda: not pid_alive(codex_pid) and not pid_alive(child_pid), timeout=10)


def test_resume_is_limited_to_ids_issued_for_the_session_key(start_sidecar):
    srv = start_sidecar(provider="codex")
    first = converse(srv.port, "k-owner")
    precondition(first.events[0] == ("session", {"sessionId": THREAD_ID}), f"{first.events}")

    own = _converse_resume(srv, "k-owner", THREAD_ID)
    # The same thread id sent under another sessionKey must be refused up front.
    foreign = srv.post_json(
        "/v1/converse", {"sessionKey": "k-intruder", "prompt": "x", "sessionId": THREAD_ID}
    )

    assert own == 200
    assert foreign[0] == 400
    assert foreign[1]["code"] == "bad_request"


@pytest.mark.parametrize("resume", ["fork", "same"])
def test_resume_chain_follows_whatever_id_codex_reports(start_sidecar, resume):
    # codex may report a new (forked) or the same thread id on resume; the latest one
    # must stay resumable by the same sessionKey, turn after turn.
    srv = start_sidecar(provider="codex", FAKE_CODEX_RESUME=resume)

    first = converse(srv.port, "k-chain")
    second = _resume(srv, "k-chain", first.events[0][1]["sessionId"])
    third = _resume(srv, "k-chain", second.events[0][1]["sessionId"])

    ids = [r.events[0][1]["sessionId"] for r in (first, second, third)]
    assert len(set(ids)) == (3 if resume == "fork" else 1)
    assert [r.terminal_events for r in (first, second, third)] == [["done"]] * 3


def _resume(srv, session_key: str, session_id: str):
    import socket

    from .harness import BEARER, StreamResult, parse_sse_frames

    body = json.dumps({"sessionKey": session_key, "prompt": "more", "sessionId": session_id})
    request = (
        f"POST /v1/converse HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {BEARER}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + body.encode()
    raw = b""
    with socket.create_connection(("127.0.0.1", srv.port), timeout=10) as sock:
        sock.sendall(request)
        while chunk := sock.recv(65536):
            raw += chunk
    head, _, rest = raw.partition(b"\r\n\r\n")
    result = StreamResult(status=int(head.split()[1]))
    precondition(result.status == 200, f"resume refused: {raw[:300]!r}")
    # Chunk framing is irrelevant here; the SSE frames are intact in the body text.
    result.events, _ = parse_sse_frames(rest.decode())
    return result
