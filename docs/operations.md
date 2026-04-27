# Claude Sidecar — Operations Guide

Operational reference for running the sidecar in production. The HTTP+SSE
contract itself lives in `openapi.yaml`.

## Environment variables

| Var | Default | Description |
|---|---|---|
| `BIND` | `127.0.0.1` | Listen address. Use `127.0.0.1` for Pod-local sidecar; `0.0.0.0` only for standalone testing. |
| `PORT` | `7300` | Listen port. |
| `BEARER_SECRET` | **required** | Shared secret expected in `Authorization: Bearer …`. Provision via Kubernetes Secret. |
| `MAX_CONCURRENT` | `8` | Global in-flight `/v1/converse` cap; further requests get HTTP 429 (`busy`). |
| `TURN_TIMEOUT_SEC` | `90` | Hard ceiling per turn. Past this, the SDK subprocess is SIGKILL'd and the SSE stream emits `error: timeout`. |
| `CANCEL_GRACE_SEC` | `5` | Grace window between `/cancel` and force-cancel of the underlying task. |
| `SHUTDOWN_GRACE_SEC` | `10` | Window during process shutdown for in-flight turns to drain before force-cancel. Also sets `uvicorn --timeout-graceful-shutdown`. |
| `WORKSPACE_ROOT` | `/var/lib/claude-sidecar/sessions` | Per-`sessionKey` workspace root (session mode). Stateless mode uses an OS temp dir. |
| `CLAUDE_MD_PATH` | unset | Path to the static base system prompt (typically a ConfigMap mount, e.g. `/workspace/CLAUDE.md`). |
| `MCP_CONFIG_PATH` | unset | Path to `mcp.json` forwarded to the Agent SDK (typically `/etc/sidecar/mcp.json`). |
| `CLAUDE_CODE_OAUTH_TOKEN` | unset | Long-lived subscription token from `claude setup-token`. Recommended over the file-mount approach for sidecars. |
| `CLAUDE_AUTH_PATH` | `~/.claude.json` | Subscription auth file location (alternative to the env var). Used by `/readyz` validation. |
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

**One Pod = one Anthropic identity.** The sidecar runs `claude` in
subscription mode. The CLI (and the Agent SDK that wraps it) accepts any of
the following as identity, picked in this order:

| Source | When to use |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` env var | **Recommended for sidecars.** Long-lived (1 year) token produced by `claude setup-token`. |
| `~/.claude.json` file | Alternative — interactive-login OAuth state. Rotates frequently; you must refresh the Secret each time. |
| `ANTHROPIC_API_KEY` env var | Pay-as-you-go API mode, bypasses the subscription. |

`/readyz` returns 200 when **any** of these is present.

### Recommended: `claude setup-token` → env var

1. On a host with a browser and an active Claude subscription
   (Pro / Max / Team / Enterprise), install the CLI:

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

3. Provision it as a Kubernetes Secret:

   ```bash
   kubectl create secret generic anthropic-claude-auth \
       --from-literal=CLAUDE_CODE_OAUTH_TOKEN='<paste-token-here>'
   ```

4. `deploy/k8s/deployment.yaml` already loads `CLAUDE_CODE_OAUTH_TOKEN` from
   that Secret as an env var on the sidecar container.

5. **Refresh annually.** Re-run `claude setup-token` before the previous
   token expires and `kubectl apply` the updated Secret. The Pod picks up
   the new value on its next restart.

Treat the token like a password — anyone with it can spend your
subscription quota.

### Alternative: mount `~/.claude.json`

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

`X-User-Id` (caller-supplied) is forwarded verbatim to MCP servers for
per-user attribution; the model never sees it.

## Consumer contract: writing the MCP server

The sidecar is the MCP **client**. Domain operations live in MCP **servers**
that the consumer (the app that talks to the sidecar) implements. Examples:

- Spring AI MCP starter (`/mcp/sse`)
- FastAPI MCP libraries
- Go MCP libraries

Point the sidecar at your MCP server in `mcp.json`:

```json
{
  "mcpServers": {
    "domain-tools": {
      "type": "sse",
      "url": "http://localhost:8080/mcp/sse",
      "headers": { "Authorization": "Bearer ${MCP_BEARER}" }
    }
  }
}
```

**Identity propagation:** read identity from request scope (header / TLS /
cookie). **Do not** accept user IDs as MCP tool arguments — that opens a
prompt-injection vector where the model invents IDs.

## Health & metrics

| Endpoint | Auth | Purpose |
|---|---|---|
| `/healthz` | none | Liveness — process responding. |
| `/readyz` | none | Readiness — `claude` binary on PATH **and** Anthropic identity present. 503 with `detail` when not ready. |
| `/metrics` | none | Prometheus text format with five collectors. |

Metrics:

| Name | Type | Labels |
|---|---|---|
| `sidecar_requests_total` | counter | `outcome` ∈ `{ok, busy, timeout, sdk_error, internal, cancelled}` |
| `sidecar_request_duration_seconds` | histogram | `outcome` |
| `sidecar_inflight` | gauge | — |
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

## Shutdown

- `SIGTERM` (k8s rolling restart, scale down):
  1. uvicorn stops accepting new connections.
  2. Lifespan shutdown calls `InflightRegistry.drain()` → every in-flight turn
     receives a cancel event → SSE generators emit `error: cancelled` and
     close.
  3. After `SHUTDOWN_GRACE_SEC` any survivors are force-cancelled.
- For long turns, set `SHUTDOWN_GRACE_SEC` higher than `terminationGracePeriodSeconds` is **not** advised — k8s will SIGKILL the pod
  first. Keep `SHUTDOWN_GRACE_SEC` ≤ pod grace period minus 5 s.

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
