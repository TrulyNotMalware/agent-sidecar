"""Turn lifecycle against a real server: terminal frames, cancel, disconnect, timeout, shutdown.

Tests marked known_bug reproduce open bugs (finding ID first in the reason). They are
expected to fail today; the change that fixes a finding removes its marker. Setup
steps use precondition()/BackgroundConverse so a broken harness fails the run
instead of passing as the known bug.
"""

import shutil
import signal
import time

import pytest

from .harness import (
    BackgroundConverse,
    converse,
    known_bug,
    pid_alive,
    precondition,
    wait_until,
)

pytestmark = pytest.mark.integration


def test_normal_turn_streams_session_text_done(start_sidecar):
    srv = start_sidecar(mode="normal")

    r = converse(srv.port, "k-normal")

    assert r.status == 200
    assert r.event_names == ["session", "text", "done"]
    # "final" comes from fake_claude.py: proves the fake, not a real CLI, answered.
    assert r.events[-1][1]["finalText"] == "final"
    assert r.terminated


def test_turn_timeout_emits_timeout_frame(start_sidecar):
    srv = start_sidecar(mode="hang", TURN_TIMEOUT_SEC="2")

    r = converse(srv.port, "k-timeout")

    assert r.terminal_events == ["error"]
    assert r.events[-1][1]["code"] == "timeout"
    assert r.terminated


@known_bug("A10: SDK close() delays the timeout frame ~5s past TURN_TIMEOUT_SEC")
def test_turn_timeout_is_reported_promptly(start_sidecar):
    srv = start_sidecar(mode="hang", TURN_TIMEOUT_SEC="2")

    r = converse(srv.port, "k-timeout-prompt")

    assert r.terminal_events == ["error"]
    assert r.elapsed < 2 + 3


@known_bug("A6: a CLI failure after the result emits `done` and then `error`")
def test_failure_after_result_keeps_a_single_terminal_frame(start_sidecar):
    srv = start_sidecar(mode="result_then_fail")

    r = converse(srv.port, "k-result-then-fail")

    assert r.terminal_events == ["done"]
    assert r.terminated


@known_bug("A6: a runner that ends without a result emits no terminal frame")
def test_exit_without_result_emits_error_terminal(start_sidecar):
    srv = start_sidecar(mode="no_result")

    r = converse(srv.port, "k-no-result")

    assert r.terminal_events == ["error"]
    assert r.events[-1][1]["code"] == "sdk_error"
    assert r.terminated


@known_bug("A2/A3: cancelling a silent turn cuts the stream without `error: cancelled`")
def test_cancel_of_silent_turn_emits_cancelled_frame(start_sidecar):
    srv = start_sidecar(mode="hang", CANCEL_GRACE_SEC="1")
    bg = BackgroundConverse(srv.port, "k-cancel")
    bg.wait_first_event(10)

    status, body = srv.post_json("/v1/sessions/k-cancel/cancel")
    precondition(status == 202, f"cancel returned {status}: {body!r}")
    r = bg.result(timeout=30)

    assert r.terminal_events == ["error"]
    assert r.events[-1][1]["code"] == "cancelled"
    assert r.terminated


@known_bug("A4: a client disconnect leaves the CLI running and frees the sessionKey early")
def test_client_disconnect_terminates_cli_before_releasing_session(start_sidecar):
    srv = start_sidecar(mode="hang")

    first = converse(srv.port, "k-disconnect", disconnect_after=1)
    precondition(first.event_names == ["session"], f"first stream got {first.event_names}")
    precondition(wait_until(lambda: len(srv.cli_pids()) == 1, timeout=5), "no CLI started")
    [cli_pid] = srv.cli_pids()
    # Give the server time to observe the disconnect; otherwise a 429 below could come
    # from the first turn simply still being registered rather than from the fix.
    time.sleep(1.0)

    # While the first CLI is still running, the same sessionKey must stay busy.
    second = converse(srv.port, "k-disconnect", read_timeout=3, disconnect_after=1)
    assert second.status == 429 or not pid_alive(cli_pid)
    # And the abandoned CLI must actually be terminated (SDK close: 5s grace, then SIGTERM).
    assert wait_until(lambda: not pid_alive(cli_pid), timeout=12)


@known_bug("A1: SIGTERM cuts in-flight streams without a terminal frame; drain() never runs")
def test_sigterm_mid_turn_ends_stream_with_terminal_frame(start_sidecar):
    srv = start_sidecar(mode="slow", FAKE_SLEEP="2", SHUTDOWN_GRACE_SEC="8")
    bg = BackgroundConverse(srv.port, "k-sigterm")
    bg.wait_first_event(10)

    srv.proc.send_signal(signal.SIGTERM)
    r = bg.result(timeout=30)
    srv.proc.wait(30)

    assert r.terminated
    assert r.terminal_events in (["done"], ["error"])
    if r.terminal_events == ["error"]:
        assert r.events[-1][1]["code"] == "cancelled"
    assert wait_until(lambda: not srv.alive_cli_pids(), timeout=5)


@known_bug("A7: a workspace failure escapes before the try block (HTTP 200, zero frames)")
def test_workspace_failure_emits_error_frame(start_sidecar, tmp_path):
    srv = start_sidecar(mode="normal")
    # Replace the (valid at startup) workspace root with a regular file.
    [root] = tmp_path.glob("server-*/ws")
    shutil.rmtree(root)
    root.write_text("not a directory")

    r = converse(srv.port, "k-workspace")

    assert r.terminal_events == ["error"]
    assert r.terminated
