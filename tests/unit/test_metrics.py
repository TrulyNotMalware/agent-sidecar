def test_metrics_endpoint_exposes_prometheus_text(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    for name in (
        "sidecar_requests_total",
        "sidecar_request_duration_seconds",
        "sidecar_inflight",
        "sidecar_tool_calls_total",
        "sidecar_tokens_total",
    ):
        assert name in body, f"missing metric {name} in /metrics output"


def test_metrics_content_type(client):
    r = client.get("/metrics")
    assert r.headers["content-type"].startswith("text/plain")
