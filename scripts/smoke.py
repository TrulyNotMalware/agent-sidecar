"""Manual e2e smoke: drive `claude_runner.run_turn` against the real Claude
Agent SDK and print every event.

Requires `claude` CLI on PATH and `~/.claude.json` (or ANTHROPIC_API_KEY) so
the SDK can authenticate. Costs a small amount of subscription quota per
invocation, so it is **not** wired into pytest.

Usage:
    .venv/bin/python scripts/smoke.py
    .venv/bin/python scripts/smoke.py "your prompt here"
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from sidecar.claude_runner import run_turn

PROMPT = sys.argv[1] if len(sys.argv) > 1 else "Reply with exactly the word: PONG"


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="sidecar-smoke-") as td:
        try:
            async for ev in run_turn(
                prompt=PROMPT,
                cwd=Path(td),
                system_prompt=None,
                resume_session_id=None,
                mcp_config_path=None,
                timeout_sec=60,
            ):
                print(f"{type(ev).__name__:>15s}  {ev}")
        except Exception as exc:
            print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
