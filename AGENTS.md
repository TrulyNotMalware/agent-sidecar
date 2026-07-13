# AGENTS.md — claude-sidecar

Language-agnostic HTTP+SSE sidecar that wraps the Claude Agent SDK (and optionally OpenAI Codex)
so any service — Go, Kotlin, Java, Rust, etc. — can drive Claude's agent loop, MCP tool dispatch,
and session continuity via simple HTTP, with no SDK integration required.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│  Pod (k8s 1.28+ native sidecar)                     │
│                                                     │
│  ┌─────────────────┐      localhost:7300            │
│  │   App Container │ ──── POST /v1/converse ──┐    │
│  └─────────────────┘                          │    │
│                                               ▼    │
│  ┌─────────────────────────────────────────────┐   │
│  │          claude-sidecar (this repo)         │   │
│  │                                             │   │
│  │  FastAPI ──► ConcurrencyGate                │   │
│  │           ──► InflightRegistry              │   │
│  │           ──► claude_runner / codex_runner  │   │
│  │           ──► SSE stream (EventSource)      │   │
│  └─────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

The sidecar is domain-agnostic: it speaks `prompt`, `sessionKey`, and `X-User-Id`.
All business logic lives in MCP servers that the consumer configures and the sidecar dispatches.

---

## Directory Layout

```
sidecar/                 # Application package
├── __main__.py          # Entry: uvicorn runner
├── app.py               # FastAPI factory + lifespan
├── auth.py              # Bearer token dependency
├── claude_runner.py     # Claude Agent SDK adapter
├── codex_runner.py      # OpenAI Codex CLI adapter
├── concurrency.py       # Global / user / session gates
├── config.py            # Pydantic Settings (env-based)
├── errors.py            # ErrorCode enum + ApiError
├── inflight.py          # Cancel registry + drain
├── models.py            # ConverseRequest + response models
├── session.py           # Workspace path logic
├── sse.py               # SSE event serializer
├── routes/
│   ├── converse.py      # POST /v1/converse — main SSE stream
│   ├── cancel.py        # POST /v1/sessions/{key}/cancel
│   ├── health.py        # GET /healthz  GET /readyz
│   └── metrics.py       # GET /metrics (Prometheus)
└── observability/
    ├── logging.py       # structlog setup + redaction
    ├── metrics.py       # prometheus_client definitions
    └── tracing.py       # OpenTelemetry (lazy, optional)

tests/
├── conftest.py          # pytest fixtures (app, client, settings)
└── unit/                # 11 files, one per module

deploy/k8s/              # Kubernetes manifests
examples/                # Client examples: Python, Go, Kotlin
scripts/                 # test.sh (lint + pytest), smoke.py (e2e)
docs/operations.md       # Full operational reference
openapi.yaml             # Source-of-truth API contract
Dockerfile               # python:3.12-slim + node20 + tini
```

---

## API Reference

### `POST /v1/converse`

Auth: `Authorization: Bearer <secret>` required.

**Request body:**
```json
{
  "sessionKey": "user:abc:task-1",
  "prompt": "Summarise the meeting notes.",
  "sessionId": "<resume-id>",        // optional
  "systemPrompt": "...",             // optional – replaces CLAUDE.md
  "appendSystemPrompt": "...",       // optional – appended to CLAUDE.md
  "mode": "session"                  // "session" (default) | "stateless"
}
```

**SSE event sequence** (each frame is an `event:` line plus a `data:` line carrying the JSON below):
```
event: session      {"sessionId": "..."}
event: text         {"delta": "..."}
event: tool_use     {"name": "...", "args": {...}, "toolUseId": "..."}
event: tool_result  {"name": "...", "ok": true, "toolUseId": "..."}
event: done         {"finalText": "...", "usage": {"inputTokens": N, "outputTokens": N, "cacheReadInputTokens": N, "cacheCreationInputTokens": N}}
event: error        {"code": "...", "message": "..."}   ← terminal, replaces done
```
Exactly one `session` first, then zero or more `text` / `tool_use` / `tool_result`,
then exactly one terminal `done` **or** `error`. Field shapes are the source-of-truth
contract in `openapi.yaml`.

### `POST /v1/sessions/{session_key}/cancel`

Returns `202` immediately. Graceful cancel (waits up to `CANCEL_GRACE_SEC`), then hard `task.cancel()`.

### `GET /healthz` — always 200
### `GET /readyz` — 200 if CLI binary + auth credential present, else 503
### `GET /metrics` — Prometheus text format

---

## Configuration (Environment Variables)

| Variable | Default | Notes |
|---|---|---|
| `BEARER_SECRET` | — | **Required** |
| `PROVIDER` | `claude` | `claude` or `codex` |
| `BIND` | `127.0.0.1` | Set `0.0.0.0` in Docker |
| `PORT` | `7300` | |
| `MAX_CONCURRENT` | `8` | Global in-flight cap |
| `TURN_TIMEOUT_SEC` | `90` | Per-turn hard timeout |
| `CANCEL_GRACE_SEC` | `5` | Soft cancel window |
| `SHUTDOWN_GRACE_SEC` | `10` | SIGTERM drain window |
| `WORKSPACE_ROOT` | `/var/lib/claude-sidecar/sessions` | Session workspaces root |
| `CLAUDE_MD_PATH` | — | Base system prompt file (hot-reloaded per request) |
| `MCP_CONFIG_PATH` | — | Path to `mcp.json` |
| `CLAUDE_CODE_OAUTH_TOKEN` | — | Claude subscription auth — **local testing only** |
| `ANTHROPIC_API_KEY` | — | Claude API auth — **production / general use** |
| `ANTHROPIC_MODE` | `subscription` | Set `api` so `/readyz` requires `ANTHROPIC_API_KEY` specifically |
| `CLAUDE_AUTH_PATH` | `~/.claude.json` | Subscription auth-file location (local dev) |
| `OPENAI_API_KEY` | — | Codex provider auth (`PROVIDER=codex`) |
| `CODEX_AUTH_PATH` | `~/.codex/auth.json` | Codex OAuth auth-file location |
| `LOG_PROMPTS` | `false` | `true` disables prompt redaction |
| `LOG_LEVEL` | `INFO` | |
| `TRACING_ENABLED` | `false` | OTel trace export |
| `OTEL_SERVICE_NAME` | `claude-sidecar` | |

---

## System Prompt Merge Rules

1. `systemPrompt` set → used directly, **replaces** `CLAUDE.md` entirely.
2. `appendSystemPrompt` set → `CLAUDE.md + "\n\n" + appendSystemPrompt`.
3. Neither → `CLAUDE.md` alone (or `None` if file absent).

---

## Concurrency & Session Model

- **One in-flight turn per `session_key`** — second request gets `429 busy`.
- **One in-flight turn per `user_id` (`X-User-Id` header)** — same constraint.
- **Global cap** — `MAX_CONCURRENT` total; excess gets `429 busy`.
- Session workspaces are SHA-256–keyed directories under `WORKSPACE_ROOT` (`root/XX/YYYY...`).
- `mode=stateless` uses a `tempfile.mkdtemp` workspace, deleted after each turn.

---

## Providers

### `claude` (default)
- Runs `@anthropic-ai/claude-code` CLI via the `claude-agent-sdk` Python package.
- Auth: `ANTHROPIC_API_KEY` for production / general use. `CLAUDE_CODE_OAUTH_TOKEN`
  (subscription) and `~/.claude.json` are for local testing only.

### `codex`
- Spawns `codex exec --json --skip-git-repo-check` (from `@openai/codex`) as a
  subprocess with `stdin=DEVNULL`, then parses the NDJSON event stream.
  `--skip-git-repo-check` is required because session workspaces are plain scratch
  dirs (not git repos); `stdin=DEVNULL` stops codex from blocking on "Reading
  additional input from stdin".
- Auth: `OPENAI_API_KEY`, or a `~/.codex/auth.json` written by `codex login`
  (`CODEX_AUTH_PATH` overrides the location). codex-cli does **not** read
  `OPENAI_API_KEY` at request time, so on startup (FastAPI lifespan, when
  `PROVIDER=codex`) `ensure_codex_auth()` runs `codex login --with-api-key` to
  materialize `~/.codex/auth.json` from the key — a no-op when `auth.json`
  already exists (subscription mode).
- Resume uses `codex exec resume <sessionId> <prompt>`.
- System prompt is prepended to the user prompt (no separate flag in the CLI).
- 100 KB combined-prompt hard limit (ARG_MAX guard).

---

## Observability

### Prometheus Metrics

| Metric | Type | Labels |
|---|---|---|
| `sidecar_requests_total` | Counter | `outcome` |
| `sidecar_request_duration_seconds` | Histogram | `outcome` |
| `sidecar_inflight` | Gauge | — |
| `sidecar_tool_calls_total` | Counter | `tool_name`, `outcome` |
| `sidecar_tokens_total` | Counter | `kind` (input\|output) |

> `sidecar_tool_calls_total` is labeled by `tool_name`. If you expose many distinct MCP tool
> names, apply Prometheus relabeling rules to cap cardinality.

### Structured Logging (structlog)
- JSON output by default.
- When `LOG_PROMPTS=false` (default), these keys are redacted to `"<redacted>"`:
  `prompt`, `system_prompt`, `append_system_prompt`, `delta`, `final_text`, `text`, `args`, `tool_args`.
- Empty / `None` values are **not** redacted.

### OpenTelemetry
- Activated only when `TRACING_ENABLED=true`.
- Exports via OTLP HTTP (`opentelemetry-exporter-otlp-proto-http`).
- FastAPI auto-instrumented. Per-turn span `claude.turn` carries:
  `session.key`, `session.mode`, `session.resume`, `user.id`, `tokens.input`, `tokens.output`, `outcome`.

---

## Error Model

| Code | HTTP | When |
|---|---|---|
| `bad_request` | 400 | Validation failure before stream |
| `unauthorized` | 401 | Missing / invalid Bearer token |
| `not_found` | 404 | Cancel on unknown session |
| `busy` | 429 | Concurrency limit hit |
| `timeout` | 504 | Turn exceeded `TURN_TIMEOUT_SEC` |
| `sdk_error` | 502 | CLI returned an error result |
| `internal` | 500 | Unhandled exception |
| `cancelled` | 499 | Graceful cancel acknowledged |

The **HTTP** column is the canonical mapping in `errors.py` (`ApiError.status_code`).
On `/v1/converse` only pre-stream errors are sent with that status and a JSON body:
`bad_request`, `unauthorized`, and `busy` (plus `not_found` on `/cancel`). Once the
SSE stream has opened the response is already HTTP 200, so `timeout`, `sdk_error`,
`internal`, and `cancelled` surface **only** as a terminal `event: error` frame —
their HTTP code is never put on the wire for the converse response.

`busy` is normally rejected pre-stream (a real HTTP 429 with a JSON body) via a
preflight check in the converse route. A limit hit only in the narrow race
window between preflight and in-stream registration still arrives as a terminal
SSE `error` frame with `code=busy` on an HTTP 200 stream.

`cancelled` also appears benignly when a client closes the SSE connection right
after `done`: the ASGI server cancels the turn task, and the finished turn is
logged with `outcome=cancelled` even though the client already has its answer.

---

## Development

### Prerequisites
- Python 3.12+
- Node.js 20 (for `claude` / `codex` CLI)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### Run locally
```bash
BEARER_SECRET=dev-secret python -m sidecar
```

### Lint + Test
```bash
ruff check .
pytest tests/ -v
```

Or via the helper script:
```bash
bash scripts/test.sh
```

### Smoke test (real CLI, consumes quota)
```bash
# Requires .env.local or .env with real credentials
python scripts/smoke.py

# Force one auth path (the other credential is scrubbed from the env first):
AUTH_MODE=subscription python scripts/smoke.py   # OAuth token / auth.json only
AUTH_MODE=api python scripts/smoke.py            # API key only
```

---

## Deployment (Kubernetes)

Uses the k8s 1.28+ **native sidecar** pattern (`initContainer` with `restartPolicy: Always`).
Both containers share the Pod network — app calls `localhost:7300`.

Key manifests:
- `deploy/k8s/deployment.yaml` — Pod spec with sidecar, probes, volume mounts.
- `deploy/k8s/secret.yaml` — Bearer token + provider auth Secrets.
- `deploy/k8s/configmap-mcp.yaml` — `mcp.json` for MCP server routing.
- `deploy/k8s/configmap-claude-md.yaml` — Base system prompt (hot-reloaded).

### Operational constraints

- **1 Pod = 1 Anthropic identity.** Multiple identities → multiple Pods.
- `WORKSPACE_ROOT` is **not GC'd** — session directories accumulate. Use an external cleanup
  policy (CronJob, emptyDir with size limit, etc.).
- `CLAUDE.md` is re-read on every request (ConfigMap hot-reload, no restart needed).
- `tini` is required as PID-1 to reap zombie claude subprocesses. Do not remove from Dockerfile.
- Set `SHUTDOWN_GRACE_SEC` to at least 5 s less than k8s `terminationGracePeriodSeconds`.

---

## Key Module Contracts

### `sidecar/concurrency.py — ConcurrencyGate`
```python
async with gate.acquire(user_id, session_key):
    ...  # raises ApiError(BUSY) if any limit exceeded

await gate.check(user_id=..., session_key=...)  # same BUSY checks, reserves nothing
```

### `sidecar/inflight.py — InflightRegistry`
```python
handle = InflightHandle(session_key, user_id, cancel_event, task)
await registry.register(handle)    # raises BUSY on duplicate
await registry.unregister(handle)  # stale handles are no-ops
await registry.drain(grace_sec)    # returns count of force-cancelled tasks
```

### `sidecar/claude_runner.py — run_turn()`
```python
async for event in run_turn(
    prompt=..., cwd=..., system_prompt=...,
    resume_session_id=..., mcp_config_path=..., timeout_sec=...
):
    # event: SessionEvent | TextEvent | ToolUseEvent | ToolResultEvent | DoneEvent
```
`claude-agent-sdk` is lazy-imported — tests without the SDK installed remain importable.

### `sidecar/session.py`
```python
workspace_for(session_key, root=settings.workspace_root)  # → Path (deterministic SHA-256 shard)

async with stateless_workspace(parent=settings.workspace_root) as ws:
    ...  # tempdir, auto-deleted on exit
```

---

## Adding a New Provider

1. Create `sidecar/<name>_runner.py` implementing async `run_turn()` with the same signature
   and yielding the same event union types as `claude_runner.py`.
2. Add the provider name to `PROVIDER` docs in `config.py`.
3. Extend `_readyz_checks()` in `sidecar/routes/health.py`.
4. Wire the runner in `sidecar/routes/converse.py` (`_get_runner()`).
5. Add a `test_health.py` case for the new `/readyz` path.
