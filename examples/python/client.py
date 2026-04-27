"""Minimal Claude Sidecar client (Python).

Streams SSE events from /v1/converse and prints them. Demonstrates the
HTTP+SSE contract — the sidecar's whole reason for being.

Requires:
    pip install httpx

Usage:
    BEARER_SECRET=... python client.py "what's the weather?"
"""

from __future__ import annotations

import json
import os
import sys

import httpx

SIDECAR_URL = os.environ.get("SIDECAR_URL", "http://127.0.0.1:7300")
BEARER = os.environ["BEARER_SECRET"]


def main(prompt: str) -> None:
    payload = {"sessionKey": "demo:python", "prompt": prompt}
    headers = {
        "Authorization": f"Bearer {BEARER}",
        "Accept": "text/event-stream",
    }
    with (
        httpx.Client(timeout=None) as client,
        client.stream(
            "POST", f"{SIDECAR_URL}/v1/converse", json=payload, headers=headers
        ) as r,
    ):
        r.raise_for_status()
        event: str | None = None
        for line in r.iter_lines():
            if not line:
                event = None
                continue
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
                print(f"[{event}] {data}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: client.py <prompt>", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1])
