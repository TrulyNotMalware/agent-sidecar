# Claude Sidecar

> HTTP+SSE bridge to the Claude Agent SDK — a drop-in Pod sidecar that lets services in any language use Claude's agent loop, MCP tool dispatch, and session continuity.

## What & why

Anthropic's **Claude Agent SDK** ships only as TypeScript and Python libraries. If your services are written in Kotlin, Java, Go, Rust, or anything else, getting the agent loop, MCP routing, and session resume means either re-implementing the SDK or going around it via the raw API. This project is the third option: a **language-agnostic sidecar** running next to your app in the same Pod, exposing the SDK as `POST /v1/converse` + SSE. Your app speaks HTTP; the sidecar drives Claude.

Domain decoupling is intentional. The sidecar knows nothing about Slack, meetings, CRM, or any business concept — its vocabulary is `prompt`, `sessionKey`, and (optionally) `X-User-Id`. Domain operations live in MCP servers **you** implement and the sidecar dispatches against.

## Quickstart

### Local

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
BEARER_SECRET=$(openssl rand -hex 32) .venv/bin/python -m sidecar
```

Default bind is `127.0.0.1:7300`. From another terminal:

```bash
curl -N -H "Authorization: Bearer $BEARER_SECRET" \
     -H "Accept: text/event-stream" \
     -d '{"sessionKey":"demo","prompt":"hello"}' \
     http://127.0.0.1:7300/v1/converse
```

### Container (k8s-style sidecar)

```bash
docker build -t claude-sidecar:1.0.0 .
```

See `deploy/k8s/` for a complete Pod manifest with k8s 1.28+ native sidecar
(`initContainer` + `restartPolicy: Always`) and the Secret / ConfigMap layout
for `ANTHROPIC_API_KEY`, `mcp.json`, and `CLAUDE.md`.

## HTTP API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/v1/converse` | Bearer | Run one turn; streams SSE events |
| `POST` | `/v1/sessions/{sessionKey}/cancel` | Bearer | Cancel an in-flight turn (graceful → SIGKILL after grace) |
| `GET` | `/healthz` | none | Liveness |
| `GET` | `/readyz` | none | Readiness — `claude` binary + auth present |
| `GET` | `/metrics` | none | Prometheus text format |

SSE event sequence: `session` → 0..N of `text` / `tool_use` / `tool_result` → terminal `done` *or* `error`. Full schema in [`openapi.yaml`](openapi.yaml).

## Architecture

```
[Your App]                              [Sidecar]                     [Claude CLI]
    |                                       |                              |
    |  POST /v1/converse  (HTTP+SSE)        |                              |
    | ------------------------------------> |  spawn child + stream-json   |
    |  { prompt, sessionKey }               | ---------------------------> |
    |  SSE: session                         |                              |
    | <------------------------------------ |                              |
    |  SSE: text.delta                      | <-- stream events            |
    |  SSE: tool_use                        | <-- MCP tool dispatched -->  | (your MCP server)
    |  SSE: tool_result / text / done       |                              |
    | <------------------------------------ |                              |
```

- **Identity:** 1 Pod = 1 Anthropic identity. **Production / general use must authenticate with `ANTHROPIC_API_KEY`.** The subscription OAuth token (`claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`) is for **local testing only** — see [Identity model](docs/operations.md#identity-model-1x).
- **Concurrency:** global `MAX_CONCURRENT` cap + per-`X-User-Id` + per-`sessionKey` gates.
- **Workspace:** `sessionKey` hashed to a sub-dir for resume; `mode=stateless` uses an ephemeral temp dir.

## Configuration

> **Auth policy:** all real deployments must use `ANTHROPIC_API_KEY` (pay-as-you-go API).
> The subscription OAuth token (`CLAUDE_CODE_OAUTH_TOKEN`, from `claude setup-token`) is
> supported **for local testing only** — never ship it to a cluster.

All via environment variables. Key ones:

| Var | Default | Purpose |
|---|---|---|
| `BEARER_SECRET` | required | Shared secret |
| `PROVIDER` | `claude` | Backend: `claude` or `codex` |
| `MAX_CONCURRENT` | `8` | Global in-flight cap |
| `TURN_TIMEOUT_SEC` | `90` | Hard ceiling per turn |
| `WORKSPACE_ROOT` | `/var/lib/claude-sidecar/sessions` | Per-session workspace root |
| `MCP_CONFIG_PATH` | unset | Path to `mcp.json` |
| `CLAUDE_MD_PATH` | unset | Static base system prompt |
| `TRACING_ENABLED` | `false` | OpenTelemetry exporter (uses standard `OTEL_*` vars) |

Full reference: [`docs/operations.md`](docs/operations.md).

## Consumer examples

Minimal clients in three languages under [`examples/`](examples/):

- [`examples/python/client.py`](examples/python/client.py) — httpx + manual SSE
- [`examples/go/client.go`](examples/go/client.go) — net/http + bufio
- [`examples/kotlin/Client.kt`](examples/kotlin/Client.kt) — OkHttp EventSource

Each is ~50 lines; the contract is meant to be trivial to adopt.

## Project layout

```
sidecar/                  # the application
├── app.py                # FastAPI factory + lifespan
├── routes/               # converse, cancel, health, metrics
├── claude_runner.py      # Agent SDK adapter (event mapping)
├── codex_runner.py       # OpenAI Codex CLI adapter (PROVIDER=codex)
├── concurrency.py        # global / user / session gates
├── inflight.py           # cancel registry + drain
├── observability/        # metrics, structured logging, OTel tracing
└── …
tests/                    # unit tests
docs/operations.md        # operations reference
examples/                 # consumer clients (Python / Go / Kotlin)
deploy/k8s/               # Pod manifests
openapi.yaml              # the contract — source of truth
```

## Development

```bash
.venv/bin/python -m pytest          # unit tests
.venv/bin/ruff check .              # lint
.venv/bin/python scripts/smoke.py   # manual e2e (uses real Claude quota)
```

Smoke-test each auth path in isolation with `AUTH_MODE`:

```bash
AUTH_MODE=subscription .venv/bin/python scripts/smoke.py   # CLAUDE_CODE_OAUTH_TOKEN only
AUTH_MODE=api .venv/bin/python scripts/smoke.py            # ANTHROPIC_API_KEY only
```

## License

MIT.

---

# Claude Sidecar (한국어)

> Claude Agent SDK를 HTTP+SSE로 노출하는 사이드카 — Pod에 같이 띄워두면 어떤 언어로 짜인 서비스든 Claude의 agent loop·MCP·세션을 그대로 쓸 수 있습니다.

## 무엇이고 왜인가

Anthropic의 **Claude Agent SDK**는 TypeScript와 Python 라이브러리만 제공됩니다. 회사 백엔드가 Kotlin/Java/Go/Rust 같은 다른 언어로 짜여 있으면 (a) 그 언어로 SDK를 재구현하거나 (b) Anthropic 원시 API를 직접 호출해서 agent loop를 직접 짜야 합니다. 이 프로젝트는 세 번째 길입니다 — **언어 무관 사이드카**가 같은 Pod에서 같이 돌면서 SDK를 `POST /v1/converse` + SSE로 노출합니다. 메인 앱은 HTTP만 칠 줄 알면 끝, 나머지는 사이드카가 처리합니다.

도메인 비결합은 의도적입니다. 사이드카는 Slack·회의·CRM 같은 비즈니스 개념을 모릅니다. 어휘는 `prompt`, `sessionKey`, (선택) `X-User-Id` 셋뿐. 도메인 연산은 **컨슈머가 작성한 MCP 서버**에 살고, 사이드카는 그것을 호출합니다.

## 빠른 시작

### 로컬

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
BEARER_SECRET=$(openssl rand -hex 32) .venv/bin/python -m sidecar
```

기본 `127.0.0.1:7300`에 바인딩됩니다. 다른 터미널에서:

```bash
curl -N -H "Authorization: Bearer $BEARER_SECRET" \
     -H "Accept: text/event-stream" \
     -d '{"sessionKey":"demo","prompt":"안녕"}' \
     http://127.0.0.1:7300/v1/converse
```

### 컨테이너 (k8s 사이드카)

```bash
docker build -t claude-sidecar:1.0.0 .
```

전체 k8s manifest 예시는 `deploy/k8s/` 참고. k8s 1.28+ native 사이드카 (`initContainer` + `restartPolicy: Always`) 패턴, `ANTHROPIC_API_KEY` / `mcp.json` / `CLAUDE.md`의 Secret · ConfigMap 레이아웃을 포함합니다.

## HTTP API

| 메서드 | 경로 | 인증 | 용도 |
|---|---|---|---|
| `POST` | `/v1/converse` | Bearer | 1턴 실행, SSE 이벤트 스트림 |
| `POST` | `/v1/sessions/{sessionKey}/cancel` | Bearer | 진행 중 turn 취소 (graceful → grace 후 SIGKILL) |
| `GET` | `/healthz` | 없음 | Liveness |
| `GET` | `/readyz` | 없음 | Readiness — `claude` 바이너리 + 인증 검증 |
| `GET` | `/metrics` | 없음 | Prometheus text 포맷 |

SSE 이벤트 순서: `session` → 0..N개 `text` / `tool_use` / `tool_result` → 종단 `done` 또는 `error`. 전체 스키마는 [`openapi.yaml`](openapi.yaml).

## 아키텍처

```
[메인 앱]                                [사이드카]                  [Claude CLI]
    |                                        |                            |
    |  POST /v1/converse  (HTTP+SSE)         |                            |
    | -------------------------------------> |  자식 프로세스 spawn       |
    |  { prompt, sessionKey }                | -------------------------> |
    |  SSE: session                          |                            |
    | <------------------------------------- |                            |
    |  SSE: text.delta                       | <-- stream-json events     |
    |  SSE: tool_use                         | <-- MCP tool 라우팅 -->    | (컨슈머의 MCP 서버)
    |  SSE: tool_result / text / done        |                            |
    | <------------------------------------- |                            |
```

- **인증**: 1 Pod = 1 Anthropic 신원. **프로덕션·일반 사용은 반드시 `ANTHROPIC_API_KEY`로 인증**합니다. 구독 OAuth 토큰(`claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`)은 **로컬 테스트 전용** — 자세한 내용은 [Identity model](docs/operations.md#identity-model-1x)
- **동시성**: 글로벌 `MAX_CONCURRENT` + per-`X-User-Id` + per-`sessionKey` 게이트
- **워크스페이스**: `sessionKey`를 해시해 sub-dir로 격리 (resume 가능). `mode=stateless`는 임시 디렉토리 사용 후 자동 정리

## 설정

> **인증 정책:** 실제 배포는 전부 `ANTHROPIC_API_KEY`(pay-as-you-go API)를 사용해야 합니다.
> 구독 OAuth 토큰(`claude setup-token`으로 만드는 `CLAUDE_CODE_OAUTH_TOKEN`)은
> **로컬 테스트 전용**입니다 — 클러스터에 절대 배포하지 마세요.

모두 환경 변수로. 핵심:

| 변수 | 기본값 | 용도 |
|---|---|---|
| `BEARER_SECRET` | 필수 | Bearer 시크릿 |
| `PROVIDER` | `claude` | 백엔드 선택: `claude` 또는 `codex` |
| `MAX_CONCURRENT` | `8` | 글로벌 동시성 상한 |
| `TURN_TIMEOUT_SEC` | `90` | turn 당 hard 타임아웃 |
| `WORKSPACE_ROOT` | `/var/lib/claude-sidecar/sessions` | 세션 워크스페이스 루트 |
| `MCP_CONFIG_PATH` | unset | `mcp.json` 경로 |
| `CLAUDE_MD_PATH` | unset | 정적 base system prompt |
| `TRACING_ENABLED` | `false` | OpenTelemetry 활성화 (`OTEL_*` 표준 환경변수 사용) |

전체 레퍼런스: [`docs/operations.md`](docs/operations.md).

## 컨슈머 예제

세 언어로 된 최소 클라이언트가 [`examples/`](examples/) 아래에 있습니다:

- [`examples/python/client.py`](examples/python/client.py) — httpx + 수동 SSE
- [`examples/go/client.go`](examples/go/client.go) — net/http + bufio
- [`examples/kotlin/Client.kt`](examples/kotlin/Client.kt) — OkHttp EventSource

각 50줄 내외. 계약 자체가 단순해서 도입 비용이 낮습니다.

## 프로젝트 구조

```
sidecar/                  # 애플리케이션
├── app.py                # FastAPI 팩토리 + lifespan
├── routes/               # converse, cancel, health, metrics
├── claude_runner.py      # Agent SDK 어댑터 (이벤트 매핑)
├── codex_runner.py       # OpenAI Codex CLI 어댑터 (PROVIDER=codex)
├── concurrency.py        # 글로벌 / user / session 게이트
├── inflight.py           # 취소 레지스트리 + drain
├── observability/        # 메트릭, 구조화 로깅, OTel 트레이싱
└── …
tests/                    # unit 테스트
docs/operations.md        # 운영 가이드
examples/                 # 컨슈머 클라이언트 (Python / Go / Kotlin)
deploy/k8s/               # Pod manifests
openapi.yaml              # 계약 — source of truth
```

## 개발

```bash
.venv/bin/python -m pytest          # unit 테스트
.venv/bin/ruff check .              # lint
.venv/bin/python scripts/smoke.py   # 수동 e2e (실제 Claude quota 소비)
```

`AUTH_MODE`로 인증 경로를 하나씩 격리해서 스모크 테스트할 수 있습니다:

```bash
AUTH_MODE=subscription .venv/bin/python scripts/smoke.py   # CLAUDE_CODE_OAUTH_TOKEN만 사용
AUTH_MODE=api .venv/bin/python scripts/smoke.py            # ANTHROPIC_API_KEY만 사용
```

## 라이선스

MIT.
