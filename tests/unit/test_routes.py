def test_converse_requires_bearer(client):
    r = client.post("/v1/converse", json={"sessionKey": "k", "prompt": "hi"})
    assert r.status_code == 401


def test_converse_rejects_wrong_bearer(client):
    r = client.post(
        "/v1/converse",
        json={"sessionKey": "k", "prompt": "hi"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401


def test_cancel_unknown_session_returns_404(client):
    r = client.post(
        "/v1/sessions/some-key/cancel",
        headers={"Authorization": "Bearer test-secret"},
    )
    assert r.status_code == 404


def test_invalid_body_returns_400_with_error_schema(client):
    r = client.post(
        "/v1/converse",
        json={"sessionKey": "", "prompt": ""},  # both empty -> validation fail
        headers={"Authorization": "Bearer test-secret"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "bad_request"
    assert "sessionKey" in body["message"] or "prompt" in body["message"]


def test_unknown_field_returns_400(client):
    r = client.post(
        "/v1/converse",
        json={"sessionKey": "k", "prompt": "hi", "unknownField": 1},
        headers={"Authorization": "Bearer test-secret"},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "bad_request"
