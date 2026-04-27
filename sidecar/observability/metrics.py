from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

REQUESTS = Counter(
    "sidecar_requests_total",
    "Total /v1/converse requests by terminal outcome.",
    labelnames=("outcome",),
)

REQUEST_DURATION = Histogram(
    "sidecar_request_duration_seconds",
    "Wall-clock duration of /v1/converse turns.",
    labelnames=("outcome",),
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 90, 120, 180, 300),
)

INFLIGHT = Gauge(
    "sidecar_inflight",
    "Currently in-flight /v1/converse turns.",
)

TOOL_CALLS = Counter(
    "sidecar_tool_calls_total",
    "MCP tool dispatches.",
    labelnames=("tool_name", "outcome"),
)

TOKENS = Counter(
    "sidecar_tokens_total",
    "Token usage reported by Claude.",
    labelnames=("kind",),
)


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
