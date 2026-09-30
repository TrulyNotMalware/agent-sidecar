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
│  │  FastAPI ──► Admission (limits, registry)   │   │
│  │          └─► SSE stream (EventSource)       │   │
│  │               │ reads Turn.events           │   │
│  │           Turn task (sidecar/turn.py)       │   │
│  │           ──► claude_runner / codex_runner  │   │
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
├── admission.py         # Limits (global / user / sessionKey), registry, drain
├── config.py            # Pydantic Settings (env-based)
├── errors.py            # ErrorCode enum + ApiError
├── events.py            # Runner events + Runner protocol (provider contract)
├── turn.py              # One turn in its own task: reservation, runner, stop/timeout
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
├── unit/                # one file per module
└── integration/         # real uvicorn + claude-agent-sdk + fake stream-json CLI (no quota)

deploy/k8s/              # Kubernetes manifests
examples/                # Client examples: Python, Go, Kotlin
scripts/                 # test.sh (lint + pytest), smoke.py (e2e)
docs/operations.md       # Full operational reference
openapi.yaml             # Source-of-truth API contract
Dockerfile               # python:3.12-slim-bookworm + Node 24 LTS + pinned codex + tini, uid 10001
constraints.txt          # runtime dependency lock used by the image
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
then exactly one terminal `done` **or** `error` — unless the turn fails before the CLI
reports its session (e.g. the CLI failing to start): then the stream is a lone `error`.
Field shapes are the source-of-truth contract in `openapi.yaml`.

### `POST /v1/sessions/{session_key}/cancel`

Returns `202` immediately. The stream ends with `error: cancelled` right away; the CLI is
closed in the background and the `sessionKey` stays busy (`429`) until it has exited.

### `GET /healthz` — always 200
### `GET /readyz` — 200 if the provider CLI (claude: the SDK's bundled binary, else PATH) + auth credential present, else 503
### `GET /metrics` — Prometheus text format

---

## Configuration (Environment Variables)

| Variable | Default | Notes |
|---|---|---|
| `BEARER_SECRET` | — | **Required** — startup fails if unset or empty |
| `PROVIDER` | `claude` | `claude` or `codex` |
| `BIND` | `127.0.0.1` | Set `0.0.0.0` in Docker |
| `PORT` | `7300` | |
| `MAX_CONCURRENT` | `8` | Global in-flight cap |
| `TURN_TIMEOUT_SEC` | `90` | Per-turn hard timeout |
| `SHUTDOWN_GRACE_SEC` | `10` | On SIGTERM, how long turns may keep streaming before `error: cancelled` (min 1) |
| `WORKSPACE_ROOT` | `/var/lib/claude-sidecar/sessions` | Session workspaces root |
| `CLAUDE_MD_PATH` | — | Base system prompt file (hot-reloaded per request) |
| `MCP_CONFIG_PATH` | — | Path to `mcp.json` (extra static servers; the image ships an empty one) |
| `MCP_SERVER_URL` | — | Streamable-HTTP MCP server scoped per turn with `X-Turn-Token` |
| `MCP_SERVER_NAME` | `domain-tools` | Per-turn server name (`[A-Za-z0-9_-]+`); tools appear as `mcp__<name>__*` |
| `CLAUDE_CODE_OAUTH_TOKEN` | — | Claude subscription auth — **local testing only** |
| `ANTHROPIC_API_KEY` | — | Claude API auth — **production / general use** |
| `ANTHROPIC_MODE` | `subscription` | Set `api` so `/readyz` requires `ANTHROPIC_API_KEY` specifically |
| `CLAUDE_AUTH_PATH` | `~/.claude.json` | Subscription auth-file location (local dev) |
| `OPENAI_API_KEY` | — | Codex provider auth (`PROVIDER=codex`) |
| `CLAUDE_TOOLS` | unset | Built-in toolset: unset = CLI default, `""` = none, else comma list (`Read,Grep`) |
| `CLAUDE_ALLOWED_TOOLS` | — | Comma list pre-approved on top of configured MCP servers (`WebFetch,Bash(git status:*)`) |
| `CLAUDE_DISALLOWED_TOOLS` | — | Comma list denied even if allowed elsewhere (deny beats allow) |
| `CLAUDE_PERMISSION_MODE` | `dontAsk` | Anything that would prompt is denied unless pre-approved |
| `CLAUDE_SETTING_SOURCES` | — | Setting sources to load (`user,project,local`); empty = none (hermetic) |
| `CLAUDE_RESTRICTED` | `false` | Opt-in CLI `--restricted`: no code-running tools/WebFetch unless `CLAUDE_TOOLS` names them; file tools confined to the workspace |
| `CODEX_AUTH_PATH` | `$CODEX_HOME/auth.json` | Codex auth-file location (leave unset; codex itself uses `$CODEX_HOME`, default `~/.codex`) |
| `CODEX_SANDBOX` | `read-only` | Always passed as `codex exec --sandbox`; `read-only` \| `workspace-write` \| `danger-full-access` |
| `CODEX_ENV_PASSTHROUGH` | — | Comma-separated extra env var names codex may inherit (e.g. a custom provider's `env_key`) |
| `LOG_PROMPTS` | `false` | `true` disables prompt redaction |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`, case-insensitive |
| `TRACING_ENABLED` | `false` | OTel trace export |
| `OTEL_SERVICE_NAME` | `claude-sidecar` | |

---

## System Prompt Merge Rules

1. `systemPrompt` set → used directly, **replaces** `CLAUDE.md` entirely.
2. `appendSystemPrompt` set (non-empty) → `CLAUDE.md + "\n\n" + appendSystemPrompt`, or just
   `appendSystemPrompt` when there is no CLAUDE.md.
3. Neither → `CLAUDE.md` alone (or `None` if file absent).

---

## Concurrency & Session Model

- **One in-flight turn per `session_key`**, in both modes (`/cancel` addresses a turn by
  its `sessionKey`) — second request gets `429 busy`.
- **One in-flight turn per `user_id` (`X-User-Id` header)** — same constraint.
- **Global cap** — `MAX_CONCURRENT` total; excess gets `429 busy`.
- Session workspaces are SHA-256–keyed directories under `WORKSPACE_ROOT` (`root/XX/YYYY...`).
- `mode=stateless` uses a temp workspace under `WORKSPACE_ROOT/.stateless/`, deleted after each turn.

---

## Providers

### `claude` (default)
- Runs the Claude Code CLI via the `claude-agent-sdk` Python package — the SDK's
  **bundled** binary, not a `claude` on PATH (`/readyz` checks the same one).
- Agent policy is explicit, not inherited from wherever the sidecar runs:
  `permission_mode=dontAsk` (would-prompt → denied), `setting_sources=[]` and
  `--strict-mcp-config` (no `~/.claude` / project settings, hooks, plugins, MCP
  servers or claude.ai connectors; managed policy settings still apply), MCP
  servers from `MCP_CONFIG_PATH` and the per-turn server pre-approved
  (`mcp__<name>`, normalized like the CLI's tool names: `v1.2` → `mcp__v1_2`). The
  built-in toolset stays the CLI default unless `CLAUDE_TOOLS` is set. See
  `CLAUDE_*` in the configuration table.
- Auth: `ANTHROPIC_API_KEY` for production / general use. `CLAUDE_CODE_OAUTH_TOKEN`
  (subscription) and `~/.claude.json` are for local testing only.

- A per-turn MCP entry carrying `X-Turn-Token` is written to a `0600` file in a
  fresh `0700` temp dir (outside the workspace) and passed to the SDK as a path —
  the SDK would otherwise inline the JSON, bearer included, on the CLI's argv.
  The file is removed when the runner finishes, which includes a stopped turn
  (timeout, cancel, client disconnect).
- `BEARER_SECRET` and `OPENAI_API_KEY` are blanked in the CLI's environment (the
  SDK can only add or override variables, not remove them).

### `codex`
- Spawns `codex exec --json --skip-git-repo-check --sandbox <CODEX_SANDBOX> -- -`
  (from `@openai/codex`) in its **own process group**, writes the prompt to stdin
  and closes it, then parses the NDJSON event stream. The prompt never appears in
  argv (no ARG_MAX limit, not visible in `ps`); closing stdin keeps codex from
  waiting for "additional input". `--skip-git-repo-check` is required because
  session workspaces are plain scratch dirs (not git repos). `--sandbox` is always
  explicit, so a `config.toml` cannot change the sandbox *mode* (its other sandbox
  settings, e.g. `writable_roots`, still apply). A `config.toml` that relied on
  `sandbox_mode = "danger-full-access"` must now set `CODEX_SANDBOX` instead.
- The resume id follows `--`, so a value starting with `-` is never parsed as a
  flag. `sessionId` must be a UUID, and for codex it must have been issued to the
  same `sessionKey` (recorded under `WORKSPACE_ROOT/.session-ids/`, outside the
  workspaces): codex resolves thread ids across every session in `CODEX_HOME`.
  On resume codex may report the same or a new (forked) thread id — both are
  recorded; continue with the latest `session` id. `mode=stateless` runs with
  `--ephemeral` and cannot be resumed.
- Stopping a turn (timeout, cancel, disconnect, shutdown) SIGTERMs codex's process
  group — the npm node wrapper, the native codex binary and anything left in their
  group — then always SIGKILLs the group after 2 s; a natural exit also sweeps it.
  codex runs model shell commands in their own session (setsid), so those are
  outside the group and codex's own responsibility. Every wait is bounded (Python
  3.12's `wait()` only returns once all pipes close).
- `type:"error"` events are not terminal (codex reports "Reconnecting… n/5" that
  way while falling back from WebSocket to HTTPS). A turn fails on `turn.failed`,
  a non-zero exit, or an exit without `turn.completed`. Oversized event lines are
  skipped; the stderr tail in error messages is the *end* of stderr.
- codex gets an **allowlisted** environment (`PATH`, `HOME`, `CODEX_HOME`, locale,
  proxy and CA variables, `OPENAI_BASE_URL`/`OPENAI_ORGANIZATION`/`OPENAI_PROJECT`,
  plus `CODEX_ENV_PASSTHROUGH`); `BEARER_SECRET` and provider API keys are withheld
  from its environment and from the shell commands it runs. Env-based codex
  credentials (`CODEX_API_KEY`, …) only work if listed in `CODEX_ENV_PASSTHROUGH`.
- This is **environment-only** isolation: the CLIs run as the sidecar's uid, so a
  tool that can run commands can still read `/proc/<sidecar pid>/environ`, other
  turns' MCP config files and `CODEX_HOME/auth.json`. Closing that needs a separate
  uid for the CLIs (tracked with the agent tool-policy work).
- Auth: `OPENAI_API_KEY`, or a `~/.codex/auth.json` written by `codex login`
  (`CODEX_AUTH_PATH` overrides the location). codex-cli does **not** read
  `OPENAI_API_KEY` at request time, so on startup (FastAPI lifespan, when
  `PROVIDER=codex`) `ensure_codex_auth()` runs `codex login --with-api-key` to
  materialize `~/.codex/auth.json` from the key — a no-op when `auth.json`
  already exists (subscription mode).
- Resume uses `codex exec resume -- <sessionId> -`.
- System prompt is prepended to the user prompt (no separate flag in the CLI).

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
- Always (whatever `LOG_PROMPTS` says), credentials are scrubbed from structlog lines
  and from plain `logging` records (uvicorn, the Agent SDK): the values of
  `BEARER_SECRET`, the provider credential variables (`ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, `OPENAI_API_KEY`, `CODEX_API_KEY`,
  `AWS_*` Bedrock credentials, `ANTHROPIC_CUSTOM_HEADERS` values), every
  `CODEX_ENV_PASSTHROUGH` variable and the request's `X-Turn-Token`, plus
  credential-shaped strings (`sk-…` keys, `Bearer`/`Basic` tokens, JWTs,
  `api_key=`/`x-api-key:` values, `user:pass@` in URLs) —
  `sidecar/observability/redaction.py`. With `LOG_PROMPTS=false` the SDK's
  "Fatal error in message reader" line (it quotes CLI output) is dropped.
- Every sidecar log line of a turn carries `turn_id` (also the `X-Turn-Id` response
  header, on 429/400 rejections too). The claude CLI's stderr is logged line by line
  as `claude.stderr`; a failed turn's details (exception text, the CLI's stderr tail)
  are `error_detail` on `turn.closed`, and unexpected exceptions also log
  `turn.internal_error` with a traceback.

### OpenTelemetry
- Activated only when `TRACING_ENABLED=true`.
- Exports via OTLP HTTP (`opentelemetry-exporter-otlp-proto-http`).
- FastAPI auto-instrumented. Per-turn span `claude.turn` carries:
  `session.key`, `session.mode`, `session.resume`, `user.id`, `turn.id`, `tokens.input`,
  `tokens.output`, `outcome`. A failed turn sets the span status to ERROR and adds an
  `exception` event with the type and the client-facing message only — never the
  exception text or its cause chain (CLI stderr, output lines).

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
An `error` frame's `message` is what the provider reported (API status, quota,
context length — credentials scrubbed, ≤ 500 chars) or a fixed text; CLI stderr and
exception text stay in the log (`ApiError.detail`), and the message then names the
turn id (`X-Turn-Id`) to look them up with.
Every error body from a sidecar route is `{"code": ..., "message": ...}` (one `ApiError` handler in
`app.py`; validation errors are mapped to `bad_request`); `401` also sends
`WWW-Authenticate: Bearer`.
On `/v1/converse` only pre-stream errors are sent with that status and a JSON body:
`bad_request`, `unauthorized`, and `busy` (plus `not_found` on `/cancel`). Once the
SSE stream has opened the response is already HTTP 200, so `timeout`, `sdk_error`,
`internal`, and `cancelled` surface **only** as a terminal `event: error` frame —
their HTTP code is never put on the wire for the converse response.

`busy` is always a real HTTP 429: the route reserves every limit (`Admission`, one
synchronous step) before the stream opens.

Every stream ends with exactly one terminal frame: after `done`, anything the
runner reports while the CLI shuts down is logged, never sent. Timeout, cancel and
shutdown send their `error` frame immediately; the CLI is closed afterwards in the
turn's own task (see `sidecar/turn.py`). After `done` the stream stays open (silent)
until the CLI has exited, so end-of-stream means the `sessionKey` is free again.

---

## Development

### Prerequisites
- Python 3.12+
- Node.js 24 LTS + `@openai/codex` only for `PROVIDER=codex` (the claude CLI is bundled
  with `claude-agent-sdk`). `claude-agent-sdk` is pinned `>=0.2.161,<0.3` — the runners
  depend on its CLI flag semantics; bump it deliberately and rerun the full suite.

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
pytest tests/ -v                      # everything (integration tests take ~30s)
pytest tests/ -m "not integration"    # fast unit-only loop
```

`tests/integration/` starts the real server per test with the SDK pointed at
`tests/integration/fake_claude.py`, so cancel / disconnect / timeout / SIGTERM
behaviour is exercised end to end without credentials (the child gets a temp
`HOME` and an allowlisted env). Tests marked `known_bug(...)` reproduce open bugs
as strict xfails (finding ID first in the reason) — the fix for a bug must remove
its marker. Setup steps use `precondition()` so a broken harness fails the run
instead of passing as the known bug.

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
- The image runs as uid 10001; all mutable state is under `/var/lib/claude-sidecar`
  (`WORKSPACE_ROOT`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`) — mount one volume there.
- Keep k8s `terminationGracePeriodSeconds` ≥ app shutdown + `SHUTDOWN_GRACE_SEC` + 15 s: a
  native sidecar is SIGTERMed only after the app container exits (stream grace + uvicorn +
  up to 12 s for turns still closing their CLI).

---

## Key Module Contracts

### `sidecar/admission.py — Admission`
```python
admission.reserve(turn)       # sessionKey + X-User-Id + global cap, all or nothing; raises ApiError(BUSY)
admission.release(turn)       # no-op unless `turn` holds the reservation
admission.get(session_key)    # → Turn | None (the cancel route)
await admission.drain(grace_sec)  # stop all turns, wait for cleanup; returns force-cancelled count
```

### `sidecar/claude_runner.py` / `codex_runner.py` — `run_turn()`
```python
async for event in run_turn(
    prompt=..., cwd=..., system_prompt=...,
    resume_session_id=..., mcp_config_path=...,
):
    # event: SessionEvent | TextEvent | ToolUseEvent | ToolResultEvent | DoneEvent
```
The events and the `Runner` protocol (the common keyword arguments) live in
`sidecar/events.py`.
Runners have no timeout of their own; the caller bounds the turn by cancelling the
task that iterates the generator **once**, and each runner closes its CLI on the way
out (`contextlib.aclosing` at every level).

### `sidecar/turn.py — Turn`
```python
turn = Turn(session_key=..., user_id=..., admission=..., timeout_sec=...)
turn.start(open_runner, workspace)   # reserves (BUSY raises here), then own task: workspace → runner
item = await turn.events.get()       # RunnerEvent* … then TurnStopped? … then exactly one TurnEnded
turn.stop("cancelled")               # first call wins; queues TurnStopped, cancels the task once
```
`claude-agent-sdk` is lazy-imported — tests without the SDK installed remain importable.

### `sidecar/session.py`
```python
workspace_for(session_key, root=settings.workspace_root)  # → Path (deterministic SHA-256 shard)

with stateless_workspace(parent=settings.workspace_root / ".stateless") as ws:
    ...  # tempdir, auto-deleted on exit
```

---

## Adding a New Provider

1. Create `sidecar/<name>_runner.py` implementing async `run_turn()` that satisfies the
   `Runner` protocol in `sidecar/events.py` (the common keyword arguments) and yields its
   `RunnerEvent` types.
   Do not add a timeout: the turn cancels the task iterating the generator once, and the
   runner must close its CLI on the way out (`contextlib.aclosing` around every inner
   generator). Provider-specific options are bound in `_get_runner()` with
   `functools.partial`.
2. Add the provider name to `PROVIDER` docs in `config.py`.
3. Extend `_readyz_checks()` in `sidecar/routes/health.py`.
4. Wire the runner in `sidecar/routes/converse.py` (`_get_runner()`).
5. Add a `test_health.py` case for the new `/readyz` path.
