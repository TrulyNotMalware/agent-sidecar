def test_healthz_always_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "detail": None}


def test_readyz_503_when_binary_missing(client, monkeypatch):
    monkeypatch.setattr("sidecar.routes.health._binary_ready", lambda: False)
    monkeypatch.setattr("sidecar.routes.health._identity_ready", lambda s: True)
    r = client.get("/readyz")
    assert r.status_code == 503
    assert "claude binary" in r.json()["detail"]


def test_readyz_503_when_identity_missing(client, monkeypatch):
    monkeypatch.setattr("sidecar.routes.health._binary_ready", lambda: True)
    monkeypatch.setattr("sidecar.routes.health._identity_ready", lambda s: False)
    r = client.get("/readyz")
    assert r.status_code == 503
    assert "anthropic identity" in r.json()["detail"]


def test_readyz_200_when_all_good(client, monkeypatch):
    monkeypatch.setattr("sidecar.routes.health._binary_ready", lambda: True)
    monkeypatch.setattr("sidecar.routes.health._identity_ready", lambda s: True)
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def _clear_identity_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_identity_ready_subscription_with_oauth_token_env(monkeypatch, tmp_path):
    _clear_identity_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")

    from sidecar.config import Settings
    from sidecar.routes.health import _identity_ready

    s = Settings(
        bearer_secret="x",
        anthropic_mode="subscription",
        claude_auth_path=tmp_path / "missing.json",
    )
    assert _identity_ready(s) is True


def test_identity_ready_subscription_with_existing_file(tmp_path, monkeypatch):
    _clear_identity_env(monkeypatch)
    auth = tmp_path / ".claude.json"
    auth.write_text("{}")

    from sidecar.config import Settings
    from sidecar.routes.health import _identity_ready

    s = Settings(
        bearer_secret="x",
        anthropic_mode="subscription",
        claude_auth_path=auth,
    )
    assert _identity_ready(s) is True


def test_identity_ready_subscription_with_nothing(tmp_path, monkeypatch):
    _clear_identity_env(monkeypatch)

    from sidecar.config import Settings
    from sidecar.routes.health import _identity_ready

    s = Settings(
        bearer_secret="x",
        anthropic_mode="subscription",
        claude_auth_path=tmp_path / "missing.json",
    )
    assert _identity_ready(s) is False


def _codex_settings(**overrides):
    from sidecar.config import Settings

    return Settings(bearer_secret="x", provider="codex", **overrides)


def test_readyz_codex_503_when_binary_missing(client, monkeypatch):
    monkeypatch.setattr("sidecar.routes.health.get_settings", _codex_settings)
    monkeypatch.setattr("sidecar.routes.health._codex_binary_ready", lambda: False)
    monkeypatch.setattr("sidecar.routes.health._codex_identity_ready", lambda s: True)
    r = client.get("/readyz")
    assert r.status_code == 503
    assert "codex binary" in r.json()["detail"]


def test_readyz_codex_503_when_identity_missing(client, monkeypatch):
    monkeypatch.setattr("sidecar.routes.health.get_settings", _codex_settings)
    monkeypatch.setattr("sidecar.routes.health._codex_binary_ready", lambda: True)
    monkeypatch.setattr("sidecar.routes.health._codex_identity_ready", lambda s: False)
    r = client.get("/readyz")
    assert r.status_code == 503
    assert "codex identity" in r.json()["detail"]


def test_readyz_codex_200_when_all_good(client, monkeypatch):
    monkeypatch.setattr("sidecar.routes.health.get_settings", _codex_settings)
    monkeypatch.setattr("sidecar.routes.health._codex_binary_ready", lambda: True)
    monkeypatch.setattr("sidecar.routes.health._codex_identity_ready", lambda s: True)
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_codex_identity_ready_with_api_key(monkeypatch, tmp_path):
    from sidecar.routes.health import _codex_identity_ready

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    s = _codex_settings(codex_auth_path=tmp_path / "missing.json")
    assert _codex_identity_ready(s) is True


def test_codex_identity_ready_with_auth_file(monkeypatch, tmp_path):
    from sidecar.routes.health import _codex_identity_ready

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    s = _codex_settings(codex_auth_path=auth)
    assert _codex_identity_ready(s) is True


def test_codex_identity_ready_with_nothing(monkeypatch, tmp_path):
    from sidecar.routes.health import _codex_identity_ready

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    s = _codex_settings(codex_auth_path=tmp_path / "missing.json")
    assert _codex_identity_ready(s) is False


def test_identity_ready_api_with_key(monkeypatch):
    _clear_identity_env(monkeypatch)

    from sidecar.config import Settings
    from sidecar.routes.health import _identity_ready

    s = Settings(bearer_secret="x", anthropic_mode="api", anthropic_api_key="sk-test")
    assert _identity_ready(s) is True


def test_identity_ready_api_without_key(monkeypatch):
    _clear_identity_env(monkeypatch)

    from sidecar.config import Settings
    from sidecar.routes.health import _identity_ready

    s = Settings(bearer_secret="x", anthropic_mode="api", anthropic_api_key=None)
    assert _identity_ready(s) is False
