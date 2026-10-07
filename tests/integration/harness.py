"""Real-process harness: the sidecar under uvicorn, the real claude-agent-sdk, a fake CLI.

Each test starts its own server subprocess so process-level behaviour (orphaned
CLIs, SIGTERM handling, sse-starlette's global shutdown state) is observed as
it happens in production. The SSE client talks raw HTTP/1.1 so it can tell a
cleanly terminated chunked body from a connection that was cut mid-stream.
"""

from __future__ import annotations

import codecs
import contextlib
import json
import os
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypedDict, Unpack

import pytest

HERE = Path(__file__).parent
ROOT = HERE.parent.parent
BEARER = "integration-secret"


class HarnessError(RuntimeError):
    """The harness itself misbehaved. Deliberately not an AssertionError (see known_bug)."""


def known_bug(reason: str) -> pytest.MarkDecorator:
    """Mark a test that reproduces an open bug (finding ID first in the reason).

    strict=True turns an unexpected pass into a failure, so the marker has to be
    removed in the change that fixes the bug. raises=AssertionError means only the
    test's behavioural assertions count as "the known bug"; harness problems and
    failed preconditions (pytest.fail / HarnessError) still fail the run.
    """
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=reason)


def precondition(condition: bool, message: str) -> None:  # noqa: FBT001 — assert-like
    """Assert a setup step without letting its failure pass as a known_bug xfail."""
    if not condition:
        pytest.fail(f"harness precondition failed: {message}")


def pid_alive(pid: int) -> bool:
    """True while the process exists and is not a zombie."""
    if Path("/proc/self/stat").exists():  # Linux: no dependency on procps
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except (FileNotFoundError, ProcessLookupError):
            return False
        return stat.rpartition(")")[2].split()[0] != "Z"
    out = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,  # non-zero when the pid is gone: that is an answer, not an error
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
    body: Any = None  # JSON body of a non-streaming (non-200) response
    terminated: bool = False  # chunked body ended with the zero-length chunk
    timed_out: bool = False
    elapsed: float = 0.0

    @property
    def event_names(self) -> list[str]:
        return [name for name, _ in self.events]

    @property
    def terminal_events(self) -> list[str]:
        return [name for name in self.event_names if name in ("done", "error")]


def parse_sse_frames(buf: str) -> tuple[list[tuple[str, dict[str, Any]]], str]:
    """Split complete SSE frames off `buf`; returns (events, unparsed remainder).

    Comment frames (sse-starlette pings, ": ping - ...") carry no `event:` and are skipped.
    """
    events: list[tuple[str, dict[str, Any]]] = []
    *frames, rest = buf.replace("\r\n", "\n").split("\n\n")
    for frame in frames:
        name: str | None = None
        data_lines: list[str] = []
        for line in frame.split("\n"):
            if line.startswith("event:"):
                name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if name is not None:
            data = "\n".join(data_lines)
            events.append((name, json.loads(data) if data else {}))
    return events, rest


def converse(
    port: int,
    session_key: str,
    *,
    prompt: str = "hi",
    mode: str = "session",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    read_timeout: float = 30.0,
    total_timeout: float = 90.0,
    disconnect_after: int | None = None,
    on_event: Callable[[str], None] | None = None,
) -> StreamResult:
    """POST /v1/converse over a raw socket and decode the chunked SSE body.

    read_timeout bounds each read; total_timeout bounds the whole exchange (keep-alive
    pings would otherwise keep a stuck stream open forever). disconnect_after=N closes
    the socket after N SSE events (the client walks away). `body` adds request fields
    (systemPrompt, sessionId, ...).
    """
    payload = {"sessionKey": session_key, "prompt": prompt, "mode": mode, **(body or {})}
    body_bytes = json.dumps(payload).encode()
    extra = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
    request = (
        "POST /v1/converse HTTP/1.1\r\n"
        "Host: localhost\r\n"
        f"Authorization: Bearer {BEARER}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        f"{extra}"
        "\r\n"
    ).encode() + body_bytes

    started = time.monotonic()
    deadline = started + total_timeout
    sock = socket.create_connection(("127.0.0.1", port), timeout=read_timeout)
    stream = sock.makefile("rb")
    result = StreamResult(status=0)

    def arm() -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        sock.settimeout(min(read_timeout, remaining))

    try:
        sock.sendall(request)
        arm()
        result.status = int(stream.readline().split()[1])
        response_headers: dict[str, str] = {}
        while True:
            arm()
            line = stream.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            key, _, value = line.decode().partition(":")
            response_headers[key.strip().lower()] = value.strip()

        if result.status != 200:
            arm()
            length = int(response_headers.get("content-length", "0"))
            result.body = json.loads(stream.read(length) or b"null")
            return result

        decoder = codecs.getincrementaldecoder("utf-8")()
        pending = ""
        while True:
            arm()
            size_line = stream.readline()
            if not size_line:
                break  # connection closed without the terminating chunk
            size_field = size_line.split(b";")[0].strip()
            if not size_field:
                raise HarnessError(f"malformed chunk size line: {size_line!r}")
            size = int(size_field, 16)
            if size == 0:
                arm()
                stream.readline()  # CRLF after the last-chunk (no trailers are sent)
                result.terminated = True
                break
            arm()
            chunk = stream.read(size)
            pending += decoder.decode(chunk)
            frames, pending = parse_sse_frames(pending)
            for name, data in frames:
                result.events.append((name, data))
                if on_event is not None:
                    on_event(name)
            if len(chunk) < size:
                break  # cut mid-chunk
            arm()
            crlf = stream.readline()
            if not crlf:
                break
            if crlf != b"\r\n":
                raise HarnessError(f"chunk not followed by CRLF: {crlf!r}")
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


class ConverseOptions(TypedDict, total=False):
    """The converse() keyword arguments BackgroundConverse passes through."""

    prompt: str
    mode: str
    body: dict[str, Any] | None
    headers: dict[str, str] | None
    read_timeout: float
    total_timeout: float
    disconnect_after: int | None


class BackgroundConverse:
    """Run converse() on a thread so the test can act (cancel, SIGTERM) mid-stream."""

    def __init__(self, port: int, session_key: str, **kwargs: Unpack[ConverseOptions]) -> None:
        self._first_event = threading.Event()
        self._result: StreamResult | None = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run, args=(port, session_key, kwargs), daemon=True
        )
        self._thread.start()

    def _run(self, port: int, session_key: str, kwargs: ConverseOptions) -> None:
        try:
            self._result = converse(
                port, session_key, on_event=lambda _name: self._first_event.set(), **kwargs
            )
        except BaseException as exc:  # noqa: BLE001 — surfaced to the test via result()
            self._error = exc

    def wait_first_event(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while not self._first_event.wait(0.05):
            if not self._thread.is_alive() or time.monotonic() > deadline:
                pytest.fail(
                    f"harness precondition failed: no SSE event within {timeout}s "
                    f"(result={self._result!r}, error={self._error!r})"
                )

    def result(self, timeout: float) -> StreamResult:
        self._thread.join(timeout)
        if self._thread.is_alive():
            pytest.fail(f"harness: converse did not finish within {timeout}s")
        if self._error is not None:
            raise self._error
        if self._result is None:  # _run sets one of the two before the thread ends
            raise HarnessError("converse thread ended without a result")
        return self._result


@dataclass
class SidecarServer:
    port: int
    proc: subprocess.Popen[bytes]
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

    def fake_log_lines(self) -> list[tuple[int, str]]:
        """(pid, message) for every line the fake CLIs logged."""
        lines: list[tuple[int, str]] = []
        for line in self.fake_log.read_text().splitlines():
            _, pid_part, msg = line.split(" ", 2)
            lines.append((int(pid_part.removeprefix("pid=")), msg))
        return lines

    def cli_pids(self) -> list[int]:
        """PIDs of every fake CLI this server spawned, in start order."""
        return [pid for pid, msg in self.fake_log_lines() if msg.startswith("start")]

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

    def kill_process_group(self) -> None:
        """SIGKILL the server's whole process group, including CLIs orphaned by it.

        The server runs in its own session, so every descendant (even one reparented
        to init after the server exited) keeps pgid == server pid.
        """
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.proc.pid, signal.SIGKILL)


class StartSidecar(Protocol):
    """The start_sidecar fixture (conftest.py): start a server, return it once it is up."""

    def __call__(
        self,
        *,
        mode: str = ...,
        provider: str = ...,
        real_cli: bool = ...,
        **env_overrides: str,
    ) -> SidecarServer: ...
