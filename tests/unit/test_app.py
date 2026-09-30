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
