from pathlib import Path

from sidecar.session import stateless_workspace, workspace_for


def test_workspace_for_is_deterministic(tmp_path: Path):
    assert workspace_for("k1", root=tmp_path) == workspace_for("k1", root=tmp_path)


def test_workspace_for_distinguishes_keys(tmp_path: Path):
    assert workspace_for("k1", root=tmp_path) != workspace_for("k2", root=tmp_path)


def test_workspace_for_creates_directory(tmp_path: Path):
    p = workspace_for("hello", root=tmp_path)
    assert p.is_dir()


def test_stateless_workspace_creates_and_cleans_up(tmp_path: Path):
    captured: Path
    with stateless_workspace(parent=tmp_path) as ws:
        assert ws.is_dir()
        captured = ws
        (ws / "scratch.txt").write_text("data")
    assert not captured.exists()


def test_stateless_workspace_unique_per_call(tmp_path: Path):
    seen: list[Path] = []
    with stateless_workspace(parent=tmp_path) as a, stateless_workspace(parent=tmp_path) as b:
        assert a != b
        seen = [a, b]
    for p in seen:
        assert not p.exists()
