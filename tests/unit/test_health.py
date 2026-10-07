from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from sidecar.config import Settings, get_settings


def test_healthz_always_ok(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "detail": None}


def test_readyz_503_when_binary_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sidecar.routes.health._binary_ready", lambda: False)
    monkeypatch.setattr("sidecar.routes.health._identity_ready", lambda s: True)
    r = client.get("/readyz")
    assert r.status_code == 503
    assert "claude CLI" in r.json()["detail"]


def test_readyz_503_when_identity_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sidecar.routes.health._binary_ready", lambda: True)
    monkeypatch.setattr("sidecar.routes.health._identity_ready", lambda s: False)
    r = client.get("/readyz")
    assert r.status_code == 503
    assert "anthropic identity" in r.json()["detail"]


def test_readyz_200_when_all_good(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sidecar.routes.health._binary_ready", lambda: True)
    monkeypatch.setattr("sidecar.routes.health._identity_ready", lambda s: True)
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def _clear_identity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_identity_ready_subscription_with_oauth_token_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_identity_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")

    from sidecar.routes.health import _identity_ready

    s = Settings(
        bearer_secret=SecretStr("x"),
        anthropic_mode="subscription",
        claude_auth_path=tmp_path / "missing.json",
    )
    assert _identity_ready(s) is True


def test_identity_ready_subscription_with_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_identity_env(monkeypatch)
    auth = tmp_path / ".claude.json"
    auth.write_text("{}")

    from sidecar.routes.health import _identity_ready

    s = Settings(
        bearer_secret=SecretStr("x"),
        anthropic_mode="subscription",
        claude_auth_path=auth,
    )
    assert _identity_ready(s) is True


def test_identity_ready_subscription_with_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_identity_env(monkeypatch)

    from sidecar.routes.health import _identity_ready

    s = Settings(
        bearer_secret=SecretStr("x"),
        anthropic_mode="subscription",
        claude_auth_path=tmp_path / "missing.json",
    )
    assert _identity_ready(s) is False


def _codex_settings(
    codex_auth_path: Path | None = None, codex_env_passthrough: str = ""
) -> Settings:
    return Settings(
        bearer_secret=SecretStr("x"),
        provider="codex",
        codex_auth_path=codex_auth_path,
        codex_env_passthrough=codex_env_passthrough,
    )


def _use_codex_settings(app: FastAPI) -> None:
    # Settings reach the route through Depends(get_settings): override the dependency
    # with a parameterless callable (FastAPI reads the signature of an override).
    def codex_settings() -> Settings:
        return _codex_settings()

    app.dependency_overrides[get_settings] = codex_settings


def test_readyz_codex_503_when_binary_missing(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_codex_settings(app)
    monkeypatch.setattr("sidecar.routes.health._codex_binary_ready", lambda: False)
    monkeypatch.setattr("sidecar.routes.health._codex_identity_ready", lambda s: True)
    r = client.get("/readyz")
    assert r.status_code == 503
    assert "codex binary" in r.json()["detail"]


def test_readyz_codex_503_when_identity_missing(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_codex_settings(app)
    monkeypatch.setattr("sidecar.routes.health._codex_binary_ready", lambda: True)
    monkeypatch.setattr("sidecar.routes.health._codex_identity_ready", lambda s: False)
    r = client.get("/readyz")
    assert r.status_code == 503
    assert "codex identity" in r.json()["detail"]


def test_readyz_codex_200_when_all_good(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_codex_settings(app)
    monkeypatch.setattr("sidecar.routes.health._codex_binary_ready", lambda: True)
    monkeypatch.setattr("sidecar.routes.health._codex_identity_ready", lambda s: True)
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_codex_api_key_alone_is_not_ready_until_auth_json_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # codex's env is allowlisted: the key only works once startup materialized auth.json.
    from sidecar.routes.health import _codex_identity_ready

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    s = _codex_settings(codex_auth_path=tmp_path / "missing.json")
    assert _codex_identity_ready(s) is False


def test_codex_auth_file_follows_codex_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from sidecar.routes.health import _codex_identity_ready

    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    s = _codex_settings()
    assert _codex_identity_ready(s) is False

    (tmp_path / "auth.json").write_text("{}")
    assert _codex_identity_ready(s) is True


def test_codex_env_credential_counts_when_passed_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from sidecar.routes.health import _codex_identity_ready

    monkeypatch.setenv("CODEX_API_KEY", "ck-test")
    s = _codex_settings(
        codex_auth_path=tmp_path / "missing.json", codex_env_passthrough="CODEX_API_KEY"
    )
    assert _codex_identity_ready(s) is True


def test_codex_identity_ready_with_auth_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from sidecar.routes.health import _codex_identity_ready

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    s = _codex_settings(codex_auth_path=auth)
    assert _codex_identity_ready(s) is True


def test_codex_identity_ready_with_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from sidecar.routes.health import _codex_identity_ready

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    s = _codex_settings(codex_auth_path=tmp_path / "missing.json")
    assert _codex_identity_ready(s) is False


def test_identity_ready_api_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_identity_env(monkeypatch)

    from sidecar.routes.health import _identity_ready

    s = Settings(
        bearer_secret=SecretStr("x"), anthropic_mode="api", anthropic_api_key=SecretStr("sk-test")
    )
    assert _identity_ready(s) is True


def test_identity_ready_api_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_identity_env(monkeypatch)

    from sidecar.routes.health import _identity_ready

    s = Settings(bearer_secret=SecretStr("x"), anthropic_mode="api", anthropic_api_key=None)
    assert _identity_ready(s) is False


def test_claude_cli_resolves_the_sdk_bundled_binary_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The SDK runs its bundled CLI even when another `claude` is on PATH (or none is).
    from sidecar.routes.health import _claude_cli_path

    monkeypatch.setenv("PATH", "")
    path = _claude_cli_path()

    assert path is not None
    assert path.endswith("_bundled/claude")


def test_identity_file_follows_claude_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_identity_env(monkeypatch)
    from sidecar.routes.health import _identity_ready

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    s = Settings(bearer_secret=SecretStr("x"), anthropic_mode="subscription")
    assert _identity_ready(s) is False

    (tmp_path / ".claude.json").write_text("{}")
    assert _identity_ready(s) is True
