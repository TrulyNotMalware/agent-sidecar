"""Fake `claude` CLI that speaks the claude-agent-sdk stream-json protocol.

The integration harness points the real SDK at this script so turn lifecycle
(cancel, disconnect, timeout, shutdown) can be exercised without a model or
credentials. Behaviour is selected by environment variables:

    FAKE_CLAUDE_MODE   normal | slow | hang | result_then_fail | no_result
    FAKE_SLEEP         seconds used by slow / hang (default 3600)
    FAKE_IGNORE_TERM   "1" to ignore SIGTERM (simulates a CLI stuck in a tool)
    FAKE_LOG           file receiving one "<ts> pid=<pid> <msg>" line per lifecycle step
    FAKE_MAX_LIFETIME  hard self-destruct in seconds (default 120) so a killed test
                       run never leaves an hour-long hang-mode process behind
"""

import json
import os
import signal
import sys
import time

LOG = os.environ.get("FAKE_LOG", os.devnull)
SESSION_ID = "sess-fake-1"


def log(msg: str) -> None:
    with open(LOG, "a") as f:
        f.write(f"{time.time():.3f} pid={os.getpid()} {msg}\n")


def out(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def assistant(text: str) -> None:
    out({
        "type": "assistant",
        "session_id": SESSION_ID,
        "message": {"model": "fake", "content": [{"type": "text", "text": text}]},
    })


def result() -> None:
    out({
        "type": "result",
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "session_id": SESSION_ID,
        "result": "final",
        "usage": {"input_tokens": 3, "output_tokens": 4},
    })


def _on_term(*_args) -> None:
    if os.environ.get("FAKE_IGNORE_TERM") == "1":
        log("got SIGTERM (ignored)")
        return
    log("got SIGTERM")
    sys.exit(143)


def main() -> int:
    if "-v" in sys.argv or "--version" in sys.argv:
        print("9.9.999 (Claude Code)")
        return 0

    mode = os.environ.get("FAKE_CLAUDE_MODE", "normal")
    sleep_s = float(os.environ.get("FAKE_SLEEP", "3600"))
    signal.signal(signal.SIGTERM, _on_term)
    signal.alarm(int(os.environ.get("FAKE_MAX_LIFETIME", "120")))  # SIGALRM: not ignorable here
    log(f"start mode={mode} argv={json.dumps(sys.argv[1:])}")
    log(f"env BEARER_SECRET={'set' if os.environ.get('BEARER_SECRET') else 'unset'}")
    if "--mcp-config" in sys.argv:
        value = sys.argv[sys.argv.index("--mcp-config") + 1]
        if os.path.isfile(value):
            with open(value) as f:
                log(f"mcp_config_file={json.dumps({'path': value, 'content': f.read()})}")

    # SDK handshake: one control request (initialize), then the user message.
    request = json.loads(sys.stdin.readline())
    out({
        "type": "control_response",
        "response": {"subtype": "success", "request_id": request["request_id"], "response": {}},
    })
    sys.stdin.readline()
    out({"type": "system", "subtype": "init", "session_id": SESSION_ID, "uuid": "u0"})

    try:
        if mode == "normal":
            assistant("hello")
            result()
        elif mode == "slow":
            time.sleep(sleep_s)
            assistant("hello")
            time.sleep(sleep_s)
            result()
        elif mode == "hang":
            time.sleep(sleep_s)
        elif mode == "result_then_fail":
            assistant("hello")
            result()
            time.sleep(0.2)
            return 1
        elif mode == "no_result":
            assistant("hello")
        else:
            log(f"unknown mode {mode}")
            return 2
        return 0
    finally:
        log("exit")


if __name__ == "__main__":
    sys.exit(main())
