"""Manual e2e smoke: drives the runner directly against the real CLI (no HTTP server needed).

Loads .env.local then .env if present, so you can put credentials there
without touching shell state. PROVIDER selects the backend; AUTH_MODE forces
one credential path so each can be verified in isolation:

    auto          (default) whatever the CLI finds in the environment
    subscription  claude: CLAUDE_CODE_OAUTH_TOKEN / codex: ~/.codex/auth.json
                  — the API key is scrubbed from the environment first
    api           claude: ANTHROPIC_API_KEY / codex: OPENAI_API_KEY
                  — the subscription token is scrubbed from the environment first

Usage:
    cp .env.example .env.local          # fill in your token once
    .venv/bin/python scripts/smoke.py
    .venv/bin/python scripts/smoke.py "your custom prompt"
    PROVIDER=codex .venv/bin/python scripts/smoke.py "hello"
    AUTH_MODE=subscription .venv/bin/python scripts/smoke.py   # OAuth token path only
    AUTH_MODE=api .venv/bin/python scripts/smoke.py            # API key path only

Costs a small amount of quota per run — not wired into pytest.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).parent.parent


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    # .env.local takes precedence; .env is the fallback — neither overwrites
    # vars already set in the shell environment.
    load_dotenv(_ROOT / ".env.local", override=False)
    load_dotenv(_ROOT / ".env", override=False)


_load_env()

PROVIDER = os.environ.get("PROVIDER", "claude")
AUTH_MODE = os.environ.get("AUTH_MODE", "auto")
PROMPT = sys.argv[1] if len(sys.argv) > 1 else "Reply with exactly the word: PONG"


def _apply_auth_mode() -> str | None:
    """Scrub the non-selected credential so the chosen auth path is actually exercised.

    Returns an error message when the required credential is absent, else None.
    """
    if AUTH_MODE == "auto":
        return None
    if AUTH_MODE not in ("subscription", "api"):
        return f"unknown AUTH_MODE {AUTH_MODE!r} (expected auto | subscription | api)"

    if PROVIDER == "codex":
        from sidecar.codex_runner import codex_auth_file

        if AUTH_MODE == "api":
            if not os.environ.get("OPENAI_API_KEY"):
                return "AUTH_MODE=api requires OPENAI_API_KEY"
            # Same path as the server: auth.json materialized from the key, in an
            # isolated CODEX_HOME so an existing subscription login cannot mask it.
            os.environ["CODEX_HOME"] = tempfile.mkdtemp(prefix="sidecar-smoke-codex-")
        else:
            os.environ.pop("OPENAI_API_KEY", None)
            if not codex_auth_file().exists():
                return "AUTH_MODE=subscription requires $CODEX_HOME/auth.json (run `codex login`)"
        return None

    if AUTH_MODE == "api":
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return "AUTH_MODE=api requires ANTHROPIC_API_KEY"
    else:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            return (
                "AUTH_MODE=subscription requires CLAUDE_CODE_OAUTH_TOKEN "
                "(run `claude setup-token`)"
            )
    return None


async def main() -> int:
    if PROVIDER == "codex":
        from sidecar.codex_runner import run_turn
    else:
        from sidecar.claude_runner import run_turn

    auth_error = _apply_auth_mode()
    if auth_error is not None:
        print(f"FAILED: {auth_error}", file=sys.stderr)
        return 2
    try:
        return await _smoke(run_turn)
    finally:
        if PROVIDER == "codex" and AUTH_MODE == "api":
            # holds an auth.json with the API key
            shutil.rmtree(os.environ["CODEX_HOME"], ignore_errors=True)


async def _smoke(run_turn) -> int:
    if PROVIDER == "codex":
        from sidecar.codex_runner import ensure_codex_auth

        if not await ensure_codex_auth() and AUTH_MODE == "api":
            print("FAILED: `codex login --with-api-key` did not produce auth.json", file=sys.stderr)
            return 2

    print(f"provider : {PROVIDER}")
    print(f"auth     : {AUTH_MODE}")
    print(f"prompt   : {PROMPT!r}")
    print("-" * 48)

    done = False
    with tempfile.TemporaryDirectory(prefix="sidecar-smoke-") as td:
        try:
            async with asyncio.timeout(60):
                async for ev in run_turn(
                    prompt=PROMPT,
                    cwd=Path(td),
                    system_prompt=None,
                    resume_session_id=None,
                    mcp_config_path=None,
                ):
                    print(f"{type(ev).__name__:>16s}  {ev}")
                    done = done or type(ev).__name__ == "DoneEvent"
        except Exception as exc:
            print(f"\nFAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    if not done:
        print("\nFAILED: the turn ended without a DoneEvent", file=sys.stderr)
        return 1

    print("-" * 48)
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
