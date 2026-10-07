import os
import socket
import subprocess
import sys
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from .harness import BEARER, HERE, ROOT, SidecarServer, StartSidecar, wait_until

# Only these are inherited from the developer's shell; everything else (real
# credentials, PROVIDER, MCP_*, OTEL_*, a .env in the repo) is kept out.
_INHERITED_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "SYSTEMROOT")

# Readiness must not go through an HTTP proxy configured on the CI runner.
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def _fake_cli_wrapper(directory: Path, name: str, script: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    wrapper = directory / name
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{HERE / script}" "$@"\n')
    wrapper.chmod(0o755)
    return wrapper


@pytest.fixture
def start_sidecar(tmp_path: Path) -> Iterator[StartSidecar]:
    """Factory: start_sidecar(mode=..., provider=..., **ENV) -> a running SidecarServer.

    provider="claude" points the real SDK at fake_claude.py; provider="codex" puts a
    fake `codex` first on PATH (the runner spawns `codex` by name). real_cli=True
    does neither: the SDK's bundled claude / the `codex` on PATH run for real, and
    the test points them at a fake model API.
    """
    servers: list[SidecarServer] = []

    def _start(
        *,
        mode: str = "normal",
        provider: str = "claude",
        real_cli: bool = False,
        **env_overrides: str,
    ) -> SidecarServer:
        port = _free_port()
        run_dir = tmp_path / f"server-{port}"
        home = run_dir / "home"
        workspace = run_dir / "ws"
        for d in (home, workspace):
            d.mkdir(parents=True)
        fake_log = run_dir / "fake.log"
        fake_log.touch()
        server_log = run_dir / "server.log"

        env = {k: os.environ[k] for k in _INHERITED_ENV if k in os.environ}
        bin_dir = run_dir / "bin"
        if not real_cli:
            _fake_cli_wrapper(bin_dir, "codex", "fake_codex.py")
            env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env.update(
            HOME=str(home),
            CLAUDE_CONFIG_DIR=str(home / ".claude"),
            PYTHONPATH=str(ROOT),
            PYTHONUNBUFFERED="1",
            BEARER_SECRET=BEARER,
            BIND="127.0.0.1",
            PORT=str(port),
            WORKSPACE_ROOT=str(workspace),
            PROVIDER=provider,
            FAKE_CLI=""
            if real_cli
            else str(_fake_cli_wrapper(bin_dir, "claude", "fake_claude.py")),
            FAKE_LOG=str(fake_log),
            FAKE_CLAUDE_MODE=mode,
            FAKE_CODEX_MODE=mode,
            LOG_LEVEL="INFO",
            TURN_TIMEOUT_SEC="30",
            SHUTDOWN_GRACE_SEC="5",
        )
        if provider == "codex" and not real_cli:
            # codex only inherits an allowlisted env: let the fake's own knobs through.
            # (Not for claude: passthrough names are blanked in the claude CLI's env.)
            env["CODEX_ENV_PASSTHROUGH"] = (
                "FAKE_LOG,FAKE_CODEX_MODE,FAKE_CODEX_RESUME,FAKE_MAX_LIFETIME"
            )
        env.update(env_overrides)

        # Log to a file, not a pipe: an orphaned CLI inheriting a pipe would block reads.
        # cwd=run_dir keeps pydantic-settings from reading the repo's .env.
        with server_log.open("wb") as log_file:
            proc = subprocess.Popen(
                [sys.executable, str(HERE / "_server.py")],
                cwd=run_dir,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        server = SidecarServer(port=port, proc=proc, fake_log=fake_log, server_log=server_log)
        servers.append(server)

        def _ready() -> bool:
            if proc.poll() is not None:
                return True
            try:
                with _NO_PROXY_OPENER.open(f"http://127.0.0.1:{port}/healthz", timeout=0.5):
                    return True
            except OSError:
                return False

        if not wait_until(_ready, timeout=30) or proc.poll() is not None:
            pytest.fail(f"harness: sidecar did not start:\n{server_log.read_text()}")
        return server

    yield _start

    for server in servers:
        try:
            server.stop()
        finally:
            server.kill_process_group()
