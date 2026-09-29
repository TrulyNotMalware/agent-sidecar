# Claude Sidecar — Operations Guide

Operational reference for running the sidecar in production. The HTTP+SSE
contract itself lives in `openapi.yaml`.

## Environment variables

| Var | Default | Description |
|---|---|---|
| `PROVIDER` | `claude` | Backend selected per deployment: `claude` (Agent SDK) or `codex` (`codex exec`). Drives `/readyz` checks, the runner, and startup auth. |
| `BIND` | `127.0.0.1` | Listen address. Use `127.0.0.1` for Pod-local sidecar; `0.0.0.0` only for standalone testing. |
| `PORT` | `7300` | Listen port. |
| `BEARER_SECRET` | **required** | Shared secret expected in `Authorization: Bearer …`. Provision via Kubernetes Secret. Startup fails if unset or empty. |
| `MAX_CONCURRENT` | `8` | Global in-flight `/v1/converse` cap; further requests get HTTP 429 (`busy`). |
| `TURN_TIMEOUT_SEC` | `90` | Hard ceiling per turn. At the deadline the stream emits `error: timeout` and the CLI is closed in the background (see [Turn lifecycle](#turn-lifecycle-timeout-cancel-disconnect-shutdown)). |
| `SHUTDOWN_GRACE_SEC` | `10` | On SIGTERM, how long in-flight turns may keep streaming before they end with `error: cancelled` (see [Shutdown](#shutdown)). Minimum `1`. |
| `WORKSPACE_ROOT` | `/var/lib/claude-sidecar/sessions` | Per-`sessionKey` workspace root (session mode). Stateless mode uses an OS temp dir. Non-root / local runs (e.g. macOS dev) must override this to a writable directory — otherwise startup fails with `PermissionError` creating the default path. |
| `CLAUDE_MD_PATH` | unset | Path to the static base system prompt (typically a ConfigMap mount, e.g. `/workspace/CLAUDE.md`). |
| `MCP_CONFIG_PATH` | unset | Path to `mcp.json` for **extra** static MCP servers (typically `/etc/sidecar/mcp.json`). For `PROVIDER=claude` these are merged with the per-turn scoped entry; the per-turn entry wins on a name collision. |
| `MCP_SERVER_URL` | unset | Streamable-HTTP URL of the consumer's domain-tools MCP server. When set together with a per-turn `X-Turn-Token`, the sidecar injects a scoped MCP entry for both providers and forwards the token as its `Authorization` bearer. |
| `MCP_SERVER_NAME` | `codecompanion` | Name of the injected per-turn MCP server entry (the key under `mcpServers` / `-c mcp_servers.<name>`). |
| `ANTHROPIC_API_KEY` | unset | **Production / general use.** Pay-as-you-go API key from the Anthropic Console. |
| `ANTHROPIC_MODE` | `subscription` | Set to `api` in production so `/readyz` requires `ANTHROPIC_API_KEY` specifically. |
| `CLAUDE_CODE_OAUTH_TOKEN` | unset | **Local testing only.** Long-lived subscription token from `claude setup-token`. Never deploy it. |
| `CLAUDE_AUTH_PATH` | `~/.claude.json` | Subscription auth file location (local dev alternative). Used by `/readyz` validation. |
| `OPENAI_API_KEY` | unset | **`PROVIDER=codex`.** Codex API key. Materialized into `~/.codex/auth.json` at startup (see [Codex provider](#codex-provider)). |
| `CODEX_AUTH_PATH` | `~/.codex/auth.json` | Codex OAuth auth-file location, written by `codex login`. Used by `/readyz` and startup materialization. |
| `CODEX_SANDBOX` | `read-only` | **`PROVIDER=codex`.** Always passed as `codex exec --sandbox` so a `config.toml` cannot loosen it: `read-only`, `workspace-write`, or `danger-full-access`. |
| `CODEX_ENV_PASSTHROUGH` | unset | **`PROVIDER=codex`.** Comma-separated extra env var names codex may inherit (e.g. a custom model provider's `env_key`). Everything outside the built-in allowlist is withheld. |
| `LOG_PROMPTS` | `false` | When `true`, do not redact prompt/response bodies in structured logs. Default redacts. |
| `LOG_LEVEL` | `INFO` | structlog level. |
| `TRACING_ENABLED` | `false` | When `true`, enable OpenTelemetry tracing (`OTLP/HTTP`). |
| `OTEL_SERVICE_NAME` | `claude-sidecar` | Service name attached to traces. |

When `TRACING_ENABLED=true`, the standard `OTEL_EXPORTER_OTLP_ENDPOINT`,
`OTEL_EXPORTER_OTLP_HEADERS`, and friends configure the exporter. See the
OpenTelemetry SDK env reference.

## Anthropic quota & rate limits

- Quota is counted **per Anthropic account** — scaling sidecar replicas does
  **not** raise total throughput. Plan capacity at the account level.
- The sidecar's `MAX_CONCURRENT`, per-`X-User-Id`, and per-`sessionKey` gates
  protect the process from runaway in-flight requests but do not interact with
  Anthropic's server-side limits.
- A `rejected` rate-limit signal from the SDK surfaces as `error: sdk_error`
  on the SSE stream; clients should back off and retry.
- Long, tool-heavy turns count against quota; bound the worst case with
  `TURN_TIMEOUT_SEC`.

## Identity model (1.x)

**One Pod = one Anthropic identity.** The `claude` CLI (and the Agent SDK
that wraps it) accepts any of the following as identity:

| Source | When to use |
|---|---|
| `ANTHROPIC_API_KEY` env var | **Production / general use.** Pay-as-you-go API key from the Anthropic Console. |
| `CLAUDE_CODE_OAUTH_TOKEN` env var | **Local testing only.** Long-lived (1 year) token produced by `claude setup-token`. |
| `~/.claude.json` file | Local dev alternative — interactive-login OAuth state. Rotates frequently. |

`/readyz` returns 200 when **any** of these is present. Set
`ANTHROPIC_MODE=api` in production so the probe requires the API key
specifically instead of accepting a leftover local credential.

### Production / general use: `ANTHROPIC_API_KEY`

1. Create an API key in the Anthropic Console (`console.anthropic.com`).

2. Provision it as a Kubernetes Secret:

   ```bash
   kubectl create secret generic anthropic-claude-auth \
       --from-literal=ANTHROPIC_API_KEY='sk-ant-...'
   ```

3. `deploy/k8s/deployment.yaml` loads `ANTHROPIC_API_KEY` from that Secret
   and sets `ANTHROPIC_MODE=api` on the sidecar container.

API keys bill per token, are issued per workspace rather than per person,
and can be rotated or revoked from the Console without touching a browser
OAuth flow — which is why they are the only supported credential for
shared or production deployments.

### Local testing only: `claude setup-token` → env var

The subscription OAuth token spends **personal subscription quota**
(Pro / Max / Team / Enterprise) and is tied to an individual account.
Use it to smoke-test locally without burning API credit — never for
shared or production workloads, and never in a cluster Secret.

1. On a host with a browser and an active Claude subscription, install the
   CLI:

   ```bash
   npm install -g @anthropic-ai/claude-code
   ```

2. Generate a long-lived token:

   ```bash
   claude setup-token
   ```

   The command walks through OAuth in your browser and **prints a token to
   stdout**. The CLI does **not** save it anywhere — copy it now. The token
   is valid for **one year** and is scoped to inference (no Remote Control).

3. Put it in `.env.local` (gitignored) and drive it with `scripts/smoke.py`
   or a locally-run sidecar (`AUTH_MODE=subscription` forces this path).

Treat the token like a password — anyone with it can spend your
subscription quota.

### Alternative (local dev): mount `~/.claude.json`

If you prefer the interactive-login flow, run `claude` once on a host with
the subscription, copy the resulting `~/.claude.json` into a Secret, and
mount it at `$HOME` of the sidecar container (`/root` for the default
Dockerfile user). The OAuth state inside that file rotates more often than
the `setup-token` output, so plan to refresh the Secret on the same cadence
as the upstream host. `CLAUDE_AUTH_PATH` overrides the location.

### Why no API-key multi-tenancy in 1.x

Multi-tenant API key mode is intentionally not part of 1.x. To run multiple
identities, run multiple Pods, each with its own `anthropic-claude-auth`
Secret.

### `X-User-Id` propagation

`X-User-Id` (caller-supplied) is used **only** for logging, per-user
concurrency gating, and trace attribution — it is **not** forwarded to MCP
servers and the model never sees it. Per-user MCP identity instead rides in the
short-lived signed `X-Turn-Token` (see [Consumer contract](#consumer-contract-writing-the-mcp-server)):
the sidecar forwards that token as the MCP server's `Authorization` bearer, per
turn, so the server resolves identity from the token rather than a raw user id.

## Codex provider

Set `PROVIDER=codex` to run OpenAI's Codex CLI (`@openai/codex`) instead of the
Claude Agent SDK. The HTTP+SSE contract is identical; only the backend and its
auth differ.

**Auth — two options:**

| Source | When to use |
|---|---|
| `OPENAI_API_KEY` env var | Cluster / general use. |
| `~/.codex/auth.json` file | Local dev subscription — created by `codex login`. `CODEX_AUTH_PATH` overrides the path. |

`codex-cli` does **not** read `OPENAI_API_KEY` at request time — the key alone,
without the step below, produces `401`s mid-turn. So at startup (FastAPI
lifespan, when `PROVIDER=codex`) `ensure_codex_auth()` materializes the auth
file: if `~/.codex/auth.json` is absent and `OPENAI_API_KEY` is set, it runs
`codex login --with-api-key` once to write it. This is a no-op when the file
already exists (subscription mode) or no key is present. `/readyz` reports not
ready when neither the key nor the auth file is available.

In k8s the container filesystem is ephemeral, so a `codex login`-created
`auth.json` is lost on restart. Either set `OPENAI_API_KEY` (re-materialized on
every start) or mount `auth.json` as a Secret at `/root/.codex/auth.json`.

**How the runner invokes codex:** each turn runs
`codex exec --json --skip-git-repo-check --sandbox <CODEX_SANDBOX> -- <prompt>`
(or `… resume -- <sessionId> <prompt>`) with `stdin` set to `DEVNULL`.

- `--skip-git-repo-check` — session workspaces are plain scratch directories, not
  git repos, and `codex exec` refuses to run outside a trusted git repo without
  this flag.
- `stdin=DEVNULL` — an inherited stdin pipe makes codex block on "Reading
  additional input from stdin", hanging the turn until `TURN_TIMEOUT_SEC`.
- `--sandbox` — always explicit, so the sandbox for model-run shell commands is
  decided by `CODEX_SANDBOX`, not by whatever `config.toml` is present.
- `--` — ends option parsing, so a prompt or `sessionId` starting with `-`
  (`--last`, `-c sandbox_mode=…`) is never read as a flag. `sessionId` is also
  validated up front and rejected with `400` if it could look like one.
- Environment — codex (and every shell command it runs) inherits only an
  allowlist: `PATH`, `HOME`, `USER`, `SHELL`, `TERM`, locale (`LANG`, `LC_*`),
  `TMPDIR`, `CODEX_HOME`, `RUST_LOG`, `XDG_*`, CA bundles, `NODE_OPTIONS` and proxy
  variables, plus anything named in `CODEX_ENV_PASSTHROUGH` (those names are also
  blanked for the `claude` provider's CLI). `BEARER_SECRET` and provider API
  keys are withheld; auth comes from `CODEX_HOME/auth.json`.

The codex runner enforces a 100 KB combined-prompt limit (system prompt + user
prompt are concatenated, since the CLI has no separate system-prompt flag) to
guard against `ARG_MAX` exhaustion.

## Consumer contract: writing the MCP server

The sidecar is the MCP **client**. Domain operations live in MCP **servers**
that the consumer (the app that talks to the sidecar) implements. Examples:

- Spring AI MCP starter (streamable HTTP)
- FastAPI MCP libraries
- Go MCP libraries

**Per-turn scoped identity (recommended).** Rather than a single static bearer
shared by every turn, the consumer mints a **short-lived signed token per
`/v1/converse` call** and sends it as `X-Turn-Token`. Point the sidecar at the
MCP server with `MCP_SERVER_URL` (and optionally `MCP_SERVER_NAME`, default
`codecompanion`); for each turn the sidecar injects a streamable-HTTP MCP entry
for **both** providers:

- **`claude`** — a per-turn `mcp_servers` entry
  `{"type": "http", "url": MCP_SERVER_URL, "headers": {"Authorization": "Bearer <X-Turn-Token>"}}`,
  merged with any static servers and written to a `0600` file in a fresh `0700`
  temp directory outside the workspace. The SDK gets the file **path** (never on
  argv — a dict would be inlined as `--mcp-config '<json>'`); the file is deleted
  when the runner finishes.
- **`codex`** — `-c mcp_servers.<name>.url="…"` and
  `-c mcp_servers.<name>.bearer_token_env_var="CODECOMPANION_MCP_TOKEN"` config
  overrides, with the token passed only through that env var (never on argv).

The MCP server validates the token and resolves identity from its claims. The
token is short-lived, so a leak has a small blast radius, and the identity is
bound to the exact turn rather than replayable across the deployment.

`MCP_CONFIG_PATH` still points at a static `mcp.json` for **extra** servers; for
the `claude` provider those static servers are merged with the per-turn entry
(the per-turn entry wins on a name collision). When `MCP_SERVER_URL` or
`X-Turn-Token` is absent, the sidecar falls back to the static `mcp.json`
passthrough unchanged.

**Identity propagation:** read identity from the signed token (or request scope
— header / TLS / cookie). **Do not** accept user IDs as MCP tool arguments —
that opens a prompt-injection vector where the model invents IDs.

## Health & metrics

| Endpoint | Auth | Purpose |
|---|---|---|
| `/healthz` | none | Liveness — process responding. |
| `/readyz` | none | Readiness — the provider's CLI on PATH (`claude`, or `codex` when `PROVIDER=codex`) **and** a matching identity present. 503 with `detail` when not ready. |
| `/metrics` | none | Prometheus text format with five collectors. |

Metrics:

| Name | Type | Labels |
|---|---|---|
| `sidecar_requests_total` | counter | `outcome` ∈ `{ok, busy, timeout, sdk_error, internal, cancelled}` |
| `sidecar_request_duration_seconds` | histogram | `outcome` |
| `sidecar_inflight` | gauge | — (turns holding a slot, including the time spent closing their CLI) |
| `sidecar_tool_calls_total` | counter | `tool_name`, `outcome` ∈ `{started, ok, error}` |
| `sidecar_tokens_total` | counter | `kind` ∈ `{input, output}` |

`/metrics` and `/healthz` intentionally do not require the Bearer secret so
in-cluster scrapers and probes can hit them without secret distribution.

## Error model

- HTTP 4xx is returned **before** a turn starts: `400 bad_request`,
  `401 unauthorized`, `404 not_found` (cancel target absent), `429 busy`.
- After the SSE stream opens, every error is reported as the terminal
  `event: error` frame. No HTTP status changes mid-stream.
- Error codes: `timeout | sdk_error | busy | internal | cancelled`.
- `cancel` via `/v1/sessions/{sessionKey}/cancel` and a shutdown past
  `SHUTDOWN_GRACE_SEC` both end the stream with `error: cancelled`. A client that
  disconnects gets no frame (it is gone); its turn is stopped and recorded with
  `outcome=cancelled`.

## Client integration notes

- **Pin HTTP/1.1 on JDK `HttpClient` callers.** The sidecar is served by uvicorn,
  which speaks **HTTP/1.1** only. Java's `HttpClient` defaults to attempting an
  h2c (cleartext HTTP/2) upgrade on plain `http://`; uvicorn rejects the upgrade
  and the request body is dropped, surfacing as `400` with
  `body: Field required`. Set the client to `Version.HTTP_1_1` explicitly. Other
  clients that default to HTTP/1.1 (curl, most HTTP libraries) are unaffected.

## Turn lifecycle: timeout, cancel, disconnect, shutdown

Each turn runs in its own asyncio task (`sidecar/turn.py`), outside the SSE
response's task group. That task owns the concurrency reservation, the workspace
and the runner, and is cancelled at most once, so the runner always finishes
closing the CLI before the reservation is released. Consequences:

- **The terminal frame never waits for the CLI.** On timeout, cancel or shutdown
  the stream gets its `error` frame at once and ends cleanly; the CLI is closed in
  the background (claude: the SDK waits up to 5 s for it to exit, then SIGTERM, then
  SIGKILL after another 5 s).
- **The sessionKey stays busy until the CLI has exited.** A retry during that
  window gets `429 busy` instead of starting a second CLI in the same workspace.
- **Client disconnect** stops the turn the same way: the CLI is closed, never left
  running.
- **Exactly one terminal frame.** Anything the runner reports after `done` (e.g. a
  non-zero exit while the CLI shuts down) is logged, not sent; a runner that ends
  without a result gets `error: sdk_error`.

## Shutdown

- `SIGTERM` (k8s rolling restart, scale down):
  1. uvicorn stops accepting new connections.
  2. Every open stream is told the server is shutting down. In-flight turns get
     `SHUTDOWN_GRACE_SEC` − 1 s to finish normally (`done`); the rest end with
     `error: cancelled` and a clean end of stream.
  3. uvicorn waits up to `SHUTDOWN_GRACE_SEC` + 2 s for connections to close.
  4. Lifespan shutdown (`InflightRegistry.drain()`) waits up to 12 s for turns
     still closing their CLI, then force-cancels any survivors (logged as
     `shutdown.forced_cancel`; a forced turn may leave its CLI behind, which the
     container runtime reaps when PID 1 exits).
- Worst case the process needs about `SHUTDOWN_GRACE_SEC` + 15 s. Keep
  `terminationGracePeriodSeconds` at least that long (the default 30 s fits the
  default `SHUTDOWN_GRACE_SEC=10`), or k8s SIGKILLs the pod while CLIs are being
  closed.

## Operational gotchas

- **Workspace cardinality.** `WORKSPACE_ROOT` accumulates one sub-directory per
  unique `sessionKey` in session mode. Mount it on a volume that has retention
  policy / cleanup — the sidecar does not GC.
- **Tool name cardinality.** `sidecar_tool_calls_total` labels by `tool_name`.
  If your MCP exposes a vast number of distinct tool names, consider
  pre-aggregating in the consumer or relabel rules in Prometheus.
- **CLAUDE.md hot-reload.** The static base prompt is read on each request, so
  ConfigMap updates take effect on the next turn without restart.
- **PID 1 reaping.** The Dockerfile entrypoints via `tini` — do not bypass it
  in custom images, or claude subprocesses will accumulate as zombies.
