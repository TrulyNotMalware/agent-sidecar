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
    "Token usage reported by the provider CLI (input, output, cache_read, cache_creation).",
    labelnames=("kind",),
)

COST = Counter(
    "sidecar_cost_usd_total",
    "Turn cost in USD as the provider CLI estimates it (claude only; codex reports none).",
)

FIRST_TEXT = Histogram(
    "sidecar_time_to_first_text_seconds",
    "Time from the start of a turn to its first `text` event.",
    buckets=(0.25, 0.5, 1, 2, 3, 5, 10, 20, 30, 60, 120),
)


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
