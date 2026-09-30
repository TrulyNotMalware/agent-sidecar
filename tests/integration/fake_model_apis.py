"""Fake model APIs for running the *real* CLIs without credentials or quota.

    python fake_model_apis.py anthropic|openai <port> <record_dir>

Both play the same scripted turn: the first reply is a preamble text plus a call to
an MCP `echo` tool (anthropic: when the request offers such a tool; openai: when
FAKE_OPENAI_CALL_ECHO=1, since codex does not list MCP tools in the request), and
once the tool's result is in the history the reply is "final answer". Every
main-loop request body is recorded as <record_dir>/NN.json, so a test can assert
what the CLI sent (system prompt placement, tool names).

FAKE_API_ERROR=<status> makes the main-loop request fail with that HTTP status and
an error message that contains a credential-shaped string (sk-...).

FAKE_OPENAI_COMPACT_ON=<text>: the first request whose last user message contains
<text> is answered with a tool call and a huge token usage, which makes codex
auto-compact the thread (with a low model_auto_compact_token_limit) before it
continues.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

KIND, PORT, RECORD = sys.argv[1], int(sys.argv[2]), Path(sys.argv[3])
RECORD.mkdir(parents=True, exist_ok=True)
ERROR_STATUS = os.environ.get("FAKE_API_ERROR")
COMPACT_ON = os.environ.get("FAKE_OPENAI_COMPACT_ON")
ERROR_MESSAGE = "model not available for key sk-proj-leakedkey123456"
_lock = threading.Lock()
_count = 0
_compacted = False

USAGE_ANTHROPIC = {
    "input_tokens": 10, "output_tokens": 5,
    "cache_read_input_tokens": 7, "cache_creation_input_tokens": 3,
}
USAGE_OPENAI = {
    "input_tokens": 10, "input_tokens_details": {"cached_tokens": 4},
    "output_tokens": 5, "output_tokens_details": {"reasoning_tokens": 0},
    "total_tokens": 15,
}


def _record(raw: bytes) -> None:
    global _count
    with _lock:
        _count += 1
        (RECORD / f"{_count:02d}.json").write_bytes(raw)


def _sse(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n"


# --- anthropic (Messages API) --------------------------------------------------------

def _anthropic_blocks(body: dict) -> tuple[list[dict], str]:
    tools = [t.get("name", "") for t in body.get("tools", [])]
    answered = any(
        isinstance(m.get("content"), list)
        and any(b.get("type") == "tool_result" for b in m["content"])
        for m in body.get("messages", [])
    )
    echo = next((n for n in tools if n.startswith("mcp__") and n.endswith("__echo")), None)
    if answered or echo is None:
        return [{"type": "text", "text": "final answer"}], "end_turn"
    return [
        {"type": "text", "text": "preamble"},
        {"type": "tool_use", "id": "toolu_01", "name": echo, "input": {"text": "hi"}},
    ], "tool_use"


def _anthropic_message(body: dict, blocks: list[dict], stop: str | None, usage: dict) -> dict:
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": body.get("model"),
        "content": blocks, "stop_reason": stop, "stop_sequence": None, "usage": usage,
    }


def _anthropic_stream(body: dict, blocks: list[dict], stop: str, usage: dict) -> bytes:
    start = _anthropic_message(body, [], None, {**usage, "output_tokens": 1})
    out = [_sse("message_start", {"message": start})]
    for i, block in enumerate(blocks):
        if block["type"] == "text":
            opening = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": block["text"]}
        else:
            opening = {**block, "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        out.append(_sse("content_block_start", {"index": i, "content_block": opening}))
        out.append(_sse("content_block_delta", {"index": i, "delta": delta}))
        out.append(_sse("content_block_stop", {"index": i}))
    out.append(_sse("message_delta", {
        "delta": {"stop_reason": stop, "stop_sequence": None},
        "usage": {"output_tokens": usage["output_tokens"]},
    }))
    out.append(_sse("message_stop", {}))
    return "".join(out).encode()


# --- openai (Responses API, as codex calls it) ----------------------------------------

def _openai_items(body: dict) -> tuple[list[dict], dict]:
    global _compacted

    def message(text: str, i: int) -> dict:
        return {
            "type": "message", "id": f"msg_{i}", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }

    def code_call(code: str) -> dict:
        # codex exposes its tools (MCP ones too) through its `functions.exec` code tool.
        return {
            "type": "custom_tool_call", "id": "ctc_1", "call_id": "call_1",
            "namespace": "functions", "name": "exec", "status": "completed", "input": code,
        }

    items = body.get("input", [])
    answered = any(i.get("type") == "custom_tool_call_output" for i in items)
    last_user = next((json.dumps(i.get("content")) for i in reversed(items)
                      if i.get("role") == "user"), "")
    if COMPACT_ON and COMPACT_ON in last_user and not answered:
        with _lock:
            first, _compacted = not _compacted, True
        if first:
            return [message("preamble", 0), code_call("text('hi')")], {
                **USAGE_OPENAI, "input_tokens": 900_000, "total_tokens": 900_005,
            }
    if answered or os.environ.get("FAKE_OPENAI_CALL_ECHO") != "1":
        return [message("final answer", 1)], USAGE_OPENAI
    return [
        message("preamble", 0),
        code_call("text(await tools.mcp__domain_tools__echo({text: 'hi'}))"),
    ], USAGE_OPENAI


def _openai_stream(body: dict) -> bytes:
    items, usage = _openai_items(body)
    out = [_sse("response.created", {"response": {"id": "resp_1"}})]
    for idx, item in enumerate(items):
        out.append(_sse("response.output_item.done", {"output_index": idx, "item": item}))
    out.append(_sse("response.completed", {"response": {
        "id": "resp_1", "status": "completed", "output": items, "usage": usage,
    }}))
    return "".join(out).encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self) -> None:
        error = {"type": "invalid_request_error", "message": ERROR_MESSAGE}
        payload = {"type": "error", "error": error} if KIND == "anthropic" else {"error": error}
        self._send(int(ERROR_STATUS), json.dumps(payload).encode(), "application/json")

    def do_GET(self) -> None:
        self._send(404, b"", "text/plain")

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        path = self.path.split("?")[0]
        body = json.loads(raw or b"{}")
        if KIND == "anthropic" and path.endswith("/v1/messages"):
            if body.get("tools"):  # the agent loop (side requests, e.g. a title, have none)
                _record(raw)
                if ERROR_STATUS:
                    return self._error()
                blocks, stop = _anthropic_blocks(body)
                usage = USAGE_ANTHROPIC
            else:  # zero usage: a side request must not change the turn's totals
                blocks, stop = [{"type": "text", "text": "side"}], "end_turn"
                usage = dict.fromkeys(USAGE_ANTHROPIC, 0)
            if body.get("stream"):
                stream = _anthropic_stream(body, blocks, stop, usage)
                return self._send(200, stream, "text/event-stream")
            message = _anthropic_message(body, blocks, stop, usage)
            return self._send(200, json.dumps(message).encode(), "application/json")
        if KIND == "openai" and path.endswith("/responses"):
            _record(raw)
            if ERROR_STATUS:
                return self._error()
            return self._send(200, _openai_stream(body), "text/event-stream")
        self._send(404, b"", "text/plain")


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
