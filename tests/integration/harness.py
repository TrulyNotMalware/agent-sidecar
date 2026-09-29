"""Real-process harness: the sidecar under uvicorn, the real claude-agent-sdk, a fake CLI.

Each test starts its own server subprocess so process-level behaviour (orphaned
CLIs, SIGTERM handling, sse-starlette's global shutdown state) is observed as
it happens in production. The SSE client talks raw HTTP/1.1 so it can tell a
cleanly terminated chunked body from a connection that was cut mid-stream.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).parent
ROOT = HERE.parent.parent
BEARER = "integration-secret"

# Env vars that must never reach the server under test (real credentials).
_SCRUBBED_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_", "OPENAI_", "CODEX_")


def known_bug(reason: str) -> pytest.MarkDecorator:
    """Mark a test that reproduces a review.md finding which is not fixed yet.

    strict=True turns an unexpected pass into a failure, so the marker has to be
    removed in the change that fixes the bug. raises=AssertionError keeps harness
    errors (server failed to start, etc.) from being mistaken for the known bug.
    """
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=reason)


def pid_alive(pid: int) -> bool:
    """True while the process exists and is not a zombie."""
    if Path("/proc/self/stat").exists():  # Linux: no dependency on procps
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except (FileNotFoundError, ProcessLookupError):
            return False
        return stat.rpartition(")")[2].split()[0] != "Z"
    out = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return bool(out) and not out.startswith("Z")


def wait_until(predicate: Callable[[], bool], timeout: float, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@dataclass
class StreamResult:
    status: int
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    body: dict[str, Any] | None = None  # JSON body of a non-streaming (non-200) response
    terminated: bool = False  # chunked body ended with the zero-length chunk
    timed_out: bool = False
    elapsed: float = 0.0

    @property
    def event_names(self) -> list[str]:
        return [name for name, _ in self.events]

    @property
    def terminal_events(self) -> list[str]:
        return [name for name in self.event_names if name in ("done", "error")]


def _parse_sse_frames(buf: str) -> tuple[list[tuple[str, dict[str, Any]]], str]:
    events: list[tuple[str, dict[str, Any]]] = []
    *frames, rest = buf.replace("\r\n", "\n").split("\n\n")
    for frame in frames:
        name, data = None, None
        for line in frame.split("\n"):
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = line[len("data:"):].strip()
        if name is not None:
            events.append((name, json.loads(data) if data else {}))
    return events, rest


def converse(
    port: int,
    session_key: str,
    *,
    prompt: str = "hi",
    mode: str = "session",
    read_timeout: float = 30.0,
    disconnect_after: int | None = None,
    on_event: Callable[[str], None] | None = None,
) -> StreamResult:
    """POST /v1/converse over a raw socket and decode the chunked SSE body.

    disconnect_after=N closes the socket after N SSE events (client walks away).
    """
    body = json.dumps({"sessionKey": session_key, "prompt": prompt, "mode": mode}).encode()
    request = (
        "POST /v1/converse HTTP/1.1\r\n"
        "Host: localhost\r\n"
        f"Authorization: Bearer {BEARER}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "\r\n"
    ).encode() + body

    started = time.monotonic()
    sock = socket.create_connection(("127.0.0.1", port), timeout=read_timeout)
    stream = sock.makefile("rb")
    result = StreamResult(status=0)
    try:
        sock.sendall(request)
        status_line = stream.readline()
        result.status = int(status_line.split()[1])
        headers: dict[str, str] = {}
        while (line := stream.readline()) not in (b"\r\n", b"\n", b""):
            key, _, value = line.decode().partition(":")
            headers[key.strip().lower()] = value.strip()

        if result.status != 200:
            length = int(headers.get("content-length", "0"))
            result.body = json.loads(stream.read(length) or b"{}")
            return result

        pending = ""
        while True:
            size_line = stream.readline()
            if not size_line:
                break  # connection closed without the terminating chunk
            size = int(size_line.split(b";")[0].strip() or b"0", 16)
            if size == 0:
                stream.readline()
                result.terminated = True
                break
            pending += stream.read(size).decode()
            stream.readline()
            frames, pending = _parse_sse_frames(pending)
            for name, data in frames:
                result.events.append((name, data))
                if on_event is not None:
                    on_event(name)
            if disconnect_after is not None and len(result.events) >= disconnect_after:
                break
    except TimeoutError:
        result.timed_out = True
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        result.elapsed = time.monotonic() - started
        stream.close()
        sock.close()
    return result


def converse_in_background(port: int, session_key: str, **kwargs: Any):
    """Start converse() on a thread; returns (thread_result_getter, first_event_seen)."""
    first_event = threading.Event()
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["result"] = converse(
                port, session_key, on_event=lambda _name: first_event.set(), **kwargs
            )
        except BaseException as exc:  # surfaced to the test via get()
            box["error"] = exc
        finally:
            first_event.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    def get(timeout: float) -> StreamResult:
        thread.join(timeout)
        if thread.is_alive():
            raise RuntimeError(f"converse did not finish within {timeout}s")
        if "error" in box:
            raise box["error"]
        return box["result"]

    return get, first_event


@dataclass
class SidecarServer:
    port: int
    proc: subprocess.Popen
    fake_log: Path
    server_log: Path

    def post_json(self, path: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
        body = json.dumps(payload).encode() if payload is not None else b""
        request = (
            f"POST {path} HTTP/1.1\r\nHost: localhost\r\n"
            f"Authorization: Bearer {BEARER}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        ).encode() + body
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(request)
            raw = b""
            while chunk := sock.recv(65536):
                raw += chunk
        head, _, payload_bytes = raw.partition(b"\r\n\r\n")
        status = int(head.split()[1])
        try:
            return status, json.loads(payload_bytes or b"null")
        except json.JSONDecodeError:
            return status, payload_bytes.decode(errors="replace")

    def cli_pids(self) -> list[int]:
        """PIDs of every fake CLI this server spawned, in start order."""
        pids: list[int] = []
        if not self.fake_log.exists():
            return pids
        for line in self.fake_log.read_text().splitlines():
            _, pid_part, *msg = line.split(" ")
            if msg[:1] == ["start"]:
                pids.append(int(pid_part.removeprefix("pid=")))
        return pids

    def alive_cli_pids(self) -> list[int]:
        return [pid for pid in self.cli_pids() if pid_alive(pid)]

    def stop(self, sig: int = signal.SIGINT, timeout: float = 15.0) -> int | None:
        if self.proc.poll() is None:
            self.proc.send_signal(sig)
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        return self.proc.returncode

    def reap_cli_orphans(self) -> list[int]:
        """Kill fake CLIs that outlived the test so they never leak into the next one."""
        leftovers = self.alive_cli_pids()
        for pid in leftovers:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        return leftovers


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _fake_cli_wrapper(tmp_path: Path) -> Path:
    wrapper = tmp_path / "claude"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{HERE / "fake_claude.py"}" "$@"\n'
    )
    wrapper.chmod(0o755)
    return wrapper


@pytest.fixture
def start_sidecar(tmp_path: Path) -> Iterator[Callable[..., SidecarServer]]:
    servers: list[SidecarServer] = []

    def _start(*, mode: str = "normal", **env_overrides: str) -> SidecarServer:
        port = _free_port()
        workspace = tmp_path / f"ws-{port}"
        workspace.mkdir()
        fake_log = tmp_path / f"fake-{port}.log"
        fake_log.touch()
        server_log = tmp_path / f"server-{port}.log"

        env = {
            k: v for k, v in os.environ.items() if not k.startswith(_SCRUBBED_PREFIXES)
        }
        env.update(
            BEARER_SECRET=BEARER,
            BIND="127.0.0.1",
            PORT=str(port),
            WORKSPACE_ROOT=str(workspace),
            FAKE_CLI=str(_fake_cli_wrapper(tmp_path)),
            FAKE_LOG=str(fake_log),
            FAKE_CLAUDE_MODE=mode,
            LOG_LEVEL="INFO",
            TURN_TIMEOUT_SEC="30",
            CANCEL_GRACE_SEC="1",
            SHUTDOWN_GRACE_SEC="5",
            PYTHONUNBUFFERED="1",
        )
        env.update(env_overrides)

        # Log to a file, not a pipe: an orphaned CLI inheriting a pipe would block reads.
        with server_log.open("wb") as log_file:
            proc = subprocess.Popen(
                [sys.executable, str(HERE / "_server.py")],
                cwd=ROOT,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        server = SidecarServer(port=port, proc=proc, fake_log=fake_log, server_log=server_log)
        servers.append(server)

        def _ready() -> bool:
            if proc.poll() is not None:
                return True
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.5):
                    return True
            except OSError:
                return False

        if not wait_until(_ready, timeout=15) or proc.poll() is not None:
            raise RuntimeError(f"sidecar did not start:\n{server_log.read_text()}")
        return server

    yield _start

    for server in servers:
        server.stop()
        server.reap_cli_orphans()
