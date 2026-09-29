"""Fake `codex` CLI speaking the subset of `codex exec --json` the runner parses.

    FAKE_CODEX_MODE    normal | hang_with_child
    FAKE_LOG           shared with fake_claude.py: "<ts> pid=<pid> <msg>" lines
    FAKE_MAX_LIFETIME  hard self-destruct in seconds (default 120)

`hang_with_child` starts a long-running child (like a model-run shell command or a
stdio MCP server) and then blocks, so tests can check the whole process group dies.
"""

import json
import os
import signal
import subprocess
import sys
import time
import uuid

LOG = os.environ.get("FAKE_LOG", os.devnull)
THREAD_ID = "01a0ec52-3a8c-7d43-871b-149bbb8c0acf"


def log(msg: str) -> None:
    with open(LOG, "a") as f:
        f.write(f"{time.time():.3f} pid={os.getpid()} {msg}\n")


def out(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    signal.alarm(int(os.environ.get("FAKE_MAX_LIFETIME", "120")))
    argv = sys.argv[1:]
    log(f"start codex argv={json.dumps(argv)}")
    if argv[:1] == ["login"]:
        sys.stdin.read()
        return 1  # never materializes auth in tests

    prompt = sys.stdin.read() if argv and argv[-1] == "-" else None
    log(f"prompt={json.dumps(prompt)}")
    mode = os.environ.get("FAKE_CODEX_MODE", "normal")

    # Like codex 0.153: resuming forks the thread, so a *new* id is reported.
    thread_id = THREAD_ID
    if "resume" in argv:
        resumed = argv[argv.index("resume") + 2]  # resume -- <id> -
        thread_id = str(uuid.uuid5(uuid.NAMESPACE_URL, resumed))
    out({"type": "thread.started", "thread_id": thread_id})
    out({"type": "turn.started"})
    if mode == "hang_with_child":
        child = subprocess.Popen(["sleep", "3600"])
        log(f"child pid={child.pid}")
        time.sleep(3600)
    # codex reports transient trouble as type=error; the runner must not give up on it.
    out({"type": "error", "message": "Reconnecting... 1/5"})
    out({"type": "item.completed", "item": {"type": "agent_message", "text": "hello"}})
    out({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
