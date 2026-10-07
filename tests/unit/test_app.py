def test_startup_creates_cli_state_dirs_hidden_by_a_volume(monkeypatch, tmp_path):
    # A volume mounted over the image's state dir starts empty; codex refuses a
    # CODEX_HOME that does not exist, so startup must create it.
    from fastapi.testclient import TestClient

    from sidecar.app import create_app

    codex_home = tmp_path / "state" / "codex"
    claude_dir = tmp_path / "state" / "claude"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))

    with TestClient(create_app()):
        pass

    assert codex_home.is_dir()
    assert claude_dir.is_dir()


def test_codex_warns_that_static_mcp_servers_are_ignored(monkeypatch, tmp_path, capsys):
    # MCP_CONFIG_PATH servers are claude-only; codex reads its own config.toml.
    from fastapi.testclient import TestClient

    from sidecar.app import create_app
    from sidecar.config import get_settings

    static = tmp_path / "mcp.json"
    static.write_text('{"mcpServers": {"docs": {"type": "http", "url": "http://x/mcp"}}}')
    monkeypatch.setenv("PROVIDER", "codex")
    monkeypatch.setenv("MCP_CONFIG_PATH", str(static))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()
    try:
        with TestClient(create_app()):
            pass
    finally:
        get_settings.cache_clear()

    assert '"mcp.static_config_ignored"' in capsys.readouterr().out


def test_router_errors_have_the_error_schema_shape(client):
    # Unknown path and wrong method are raised by Starlette's router, outside any route.
    r = client.get("/no/such/path")
    assert r.status_code == 404
    assert r.json() == {"code": "not_found", "message": "Not Found"}

    r = client.get("/v1/converse")
    assert r.status_code == 405
    assert r.json() == {"code": "bad_request", "message": "Method Not Allowed"}
    assert "POST" in r.headers["allow"]


def test_an_unhandled_exception_is_a_json_500_without_the_exception_text(monkeypatch):
    from fastapi.testclient import TestClient

    from sidecar.app import create_app
    from sidecar.routes import health

    def boom(_settings):
        raise RuntimeError("private detail sk-proj-abcdef123456")

    monkeypatch.setattr(health, "_readyz_checks", boom)
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        r = client.get("/readyz")

    assert r.status_code == 500
    assert r.json() == {"code": "internal", "message": "internal error"}
