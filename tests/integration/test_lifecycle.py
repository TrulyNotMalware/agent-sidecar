"""Turn lifecycle against a real server: terminal frames, cancel, disconnect, timeout, shutdown.

Each test pins a lifecycle guarantee that was broken before the turn moved into its
own task (sidecar.turn); the finding ID is in the comment above the test. Setup steps
use precondition()/BackgroundConverse so a broken harness fails the run loudly.
"""

import json
import shutil
import signal
import time
from pathlib import Path

import pytest

from .harness import (
    BackgroundConverse,
    StartSidecar,
    converse,
    pid_alive,
    precondition,
    wait_until,
)

pytestmark = pytest.mark.integration


def test_normal_turn_streams_session_text_done(start_sidecar: StartSidecar) -> None:
    srv = start_sidecar(mode="normal")

    r = converse(srv.port, "k-normal")

    assert r.status == 200
    assert r.event_names == ["session", "text", "done"]
    # "final" comes from fake_claude.py: proves the fake, not a real CLI, answered.
    assert r.events[-1][1]["finalText"] == "final"
    assert r.terminated


def test_turn_timeout_emits_timeout_frame(start_sidecar: StartSidecar) -> None:
    srv = start_sidecar(mode="hang", TURN_TIMEOUT_SEC="2")

    r = converse(srv.port, "k-timeout")

    assert r.terminal_events == ["error"]
    assert r.events[-1][1]["code"] == "timeout"
    assert r.terminated


# A10: the timeout frame must not wait for the CLI to be closed.
def test_turn_timeout_is_reported_promptly(start_sidecar: StartSidecar) -> None:
    srv = start_sidecar(mode="hang", TURN_TIMEOUT_SEC="2")

    r = converse(srv.port, "k-timeout-prompt")

    assert r.terminal_events == ["error"]
    assert r.elapsed < 2 + 3


# A6: a CLI failure after the result must not add a second terminal frame.
def test_failure_after_result_keeps_a_single_terminal_frame(start_sidecar: StartSidecar) -> None:
    srv = start_sidecar(mode="result_then_fail")

    r = converse(srv.port, "k-result-then-fail")

    assert r.terminal_events == ["done"]
    assert r.terminated


# A6: a runner that ends without a result still gets a terminal frame.
def test_exit_without_result_emits_error_terminal(start_sidecar: StartSidecar) -> None:
    srv = start_sidecar(mode="no_result")

    r = converse(srv.port, "k-no-result")

    assert r.terminal_events == ["error"]
    assert r.events[-1][1]["code"] == "sdk_error"
    assert r.terminated


# A2/A3: cancel is honoured while the turn is silent, with a clean end of stream.
def test_cancel_of_silent_turn_emits_cancelled_frame(start_sidecar: StartSidecar) -> None:
    srv = start_sidecar(mode="hang")
    bg = BackgroundConverse(srv.port, "k-cancel")
    bg.wait_first_event(10)

    [cli_pid] = srv.cli_pids()

    status, body = srv.post_json("/v1/sessions/k-cancel/cancel")
    precondition(status == 202, f"cancel returned {status}: {body!r}")
    r = bg.result(timeout=30)

    assert r.terminal_events == ["error"]
    assert r.events[-1][1]["code"] == "cancelled"
    assert r.terminated
    assert r.elapsed < 5  # the frame does not wait for the CLI to exit
    assert wait_until(lambda: not pid_alive(cli_pid), timeout=12)


# A4: an abandoned CLI is terminated, and its sessionKey stays busy until then.
def test_client_disconnect_terminates_cli_before_releasing_session(
    start_sidecar: StartSidecar,
) -> None:
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


# A1: SIGTERM lets a turn finish within SHUTDOWN_GRACE_SEC.
def test_sigterm_mid_turn_ends_stream_with_terminal_frame(start_sidecar: StartSidecar) -> None:
    srv = start_sidecar(mode="slow", FAKE_SLEEP="2", SHUTDOWN_GRACE_SEC="8")
    bg = BackgroundConverse(srv.port, "k-sigterm")
    bg.wait_first_event(10)

    srv.proc.send_signal(signal.SIGTERM)
    r = bg.result(timeout=30)
    srv.proc.wait(30)

    # ~4s of turn left, 7s of grace: the turn must complete normally.
    assert r.terminated
    assert r.terminal_events == ["done"]
    assert wait_until(lambda: not srv.alive_cli_pids(), timeout=5)


# A7: a workspace failure is reported as an error frame.
def test_workspace_failure_emits_error_frame(start_sidecar: StartSidecar, tmp_path: Path) -> None:
    srv = start_sidecar(mode="normal")
    # Replace the (valid at startup) workspace root with a regular file.
    [root] = tmp_path.glob("server-*/ws")
    shutil.rmtree(root)
    root.write_text("not a directory")

    r = converse(srv.port, "k-workspace")

    assert r.terminal_events == ["error"]
    assert r.terminated


# A1: past SHUTDOWN_GRACE_SEC the stream still ends with a frame, and the CLI is closed.
def test_sigterm_past_grace_sends_cancelled_and_closes_cli(start_sidecar: StartSidecar) -> None:
    srv = start_sidecar(mode="hang", SHUTDOWN_GRACE_SEC="3")
    bg = BackgroundConverse(srv.port, "k-sigterm-grace")
    bg.wait_first_event(10)
    [cli_pid] = srv.cli_pids()

    srv.proc.send_signal(signal.SIGTERM)
    r = bg.result(timeout=30)

    assert r.terminated
    assert r.terminal_events == ["error"]
    assert r.events[-1][1]["code"] == "cancelled"
    assert r.elapsed < 3 + 5
    # Lifespan shutdown waits for the turn to close its CLI before the process exits.
    assert srv.proc.wait(30) is not None
    assert not pid_alive(cli_pid)


# End of stream means the sessionKey is free: a client chaining turns must not get 429.
@pytest.mark.parametrize(
    ("mode", "terminal"),
    [("result_then_linger", "done"), ("error_result_then_linger", "error")],
)
def test_end_of_stream_means_the_session_key_is_free(
    start_sidecar: StartSidecar, mode: str, terminal: str
) -> None:
    srv = start_sidecar(mode=mode, FAKE_SLEEP="1.5")

    first = converse(srv.port, "k-chain")
    precondition(first.terminated, f"first stream not terminated: {first.events}")
    [cli_pid] = srv.cli_pids()

    assert first.terminal_events == [terminal]
    assert not pid_alive(cli_pid)  # the stream ended only once the CLI had exited
    second = converse(srv.port, "k-chain", read_timeout=3, disconnect_after=1)
    assert second.status == 200


def test_stateless_workspace_lives_under_workspace_root_and_is_removed(
    start_sidecar: StartSidecar, tmp_path: Path
) -> None:
    srv = start_sidecar(mode="normal")

    r = converse(srv.port, "k-stateless", mode="stateless")
    precondition(r.terminal_events == ["done"], f"turn failed: {r.events}")

    [cwd_line] = [m for _p, m in srv.fake_log_lines() if m.startswith("cwd=")]
    cwd = Path(json.loads(cwd_line.removeprefix("cwd=")))
    [root] = tmp_path.glob("server-*/ws")
    assert cwd.resolve().is_relative_to((root / ".stateless").resolve())
    assert not cwd.exists()
