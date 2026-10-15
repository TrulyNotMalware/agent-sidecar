from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sidecar.config import Settings, get_settings
from sidecar.session import remember_session_id, workspace_for
from sidecar.turn import Turn

AUTH = {"Authorization": "Bearer test-secret"}
SID = "01a0ec1c-14d2-7d12-aaa7-47100d58f161"
OTHER_SID = "9b2e4c1d-0000-4000-8000-000000000000"


def _project_name(path: Path) -> str:
    return "".join(c if c.isascii() and c.isalnum() else "-" for c in str(path.resolve()))


@pytest.fixture
def claude_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    return config


@pytest.fixture
def codex_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    return path


def test_claude_session_files_are_the_project_dir_and_the_ids_entries(
    tmp_path: Path, claude_home: Path
) -> None:
    from sidecar.claude_runner import claude_session_files

    cwd = tmp_path / "ws" / "ab" / "cdef"
    project = claude_home / "projects" / _project_name(cwd)
    _touch(project / f"{SID}.jsonl")
    history = _touch(claude_home / "file-history" / SID / "v1")
    todos = _touch(claude_home / "todos" / f"{SID}-agent-{SID}.json")
    resumed_elsewhere = _touch(claude_home / "projects" / "-elsewhere" / f"{SID}.jsonl")
    foreign = _touch(claude_home / "projects" / "-elsewhere" / f"{OTHER_SID}.jsonl")

    found = claude_session_files(cwd, {SID, "sess-not-a-uuid"})

    assert set(found) == {project, history.parent, todos, resumed_elsewhere}
    assert foreign not in found


def test_a_long_workspace_path_is_found_by_session_id_only(
    tmp_path: Path, claude_home: Path
) -> None:
    from sidecar.claude_runner import claude_session_files

    cwd = tmp_path / ("d" * 250)
    transcript = _touch(claude_home / "projects" / "-cut-and-hashed" / f"{SID}.jsonl")

    # The CLI shortens such a name with a hash; guessing it could match another dir.
    assert claude_session_files(cwd, {SID}) == [transcript]


def test_codex_session_files_are_the_rollouts_of_the_ids(codex_dir: Path) -> None:
    from sidecar.codex_runner import codex_session_files

    rollout = _touch(codex_dir / "sessions/2026/10/15" / f"rollout-2026-10-15T10-00-00-{SID}.jsonl")
    archived = _touch(codex_dir / "archived_sessions" / f"rollout-2026-10-01T09-00-00-{SID}.jsonl")
    other = _touch(
        codex_dir / "sessions/2026/10/15" / f"rollout-2026-10-15T11-00-00-{OTHER_SID}.jsonl"
    )

    found = codex_session_files({SID, "*"})

    assert set(found) == {rollout, archived}
    assert other not in found


@pytest.fixture
def workspace_root(app: FastAPI, tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "sessions"
    base = get_settings()
    app.dependency_overrides[get_settings] = lambda: Settings.model_validate(
        {**base.model_dump(), "workspace_root": root}
    )
    yield root
    app.dependency_overrides.clear()


def test_delete_removes_workspace_transcripts_and_record(
    client: TestClient, workspace_root: Path, claude_home: Path
) -> None:
    from sidecar.session import known_session_ids

    workspace = workspace_for("team/task", root=workspace_root)
    _touch(workspace / "notes.md")
    project = claude_home / "projects" / _project_name(workspace)
    _touch(project / f"{SID}.jsonl")
    remember_session_id("team/task", SID, root=workspace_root)
    kept = workspace_for("other", root=workspace_root)

    r = client.delete("/v1/sessions/team/task", headers=AUTH)

    assert r.status_code == 200
    assert r.json() == {"status": "deleted", "sessionKey": "team/task"}
    assert not workspace.exists()
    assert not project.exists()
    assert known_session_ids("team/task", root=workspace_root) == set()
    assert kept.is_dir()
    # Gone means gone: a second delete finds nothing.
    assert client.delete("/v1/sessions/team/task", headers=AUTH).status_code == 404


def test_delete_of_an_unknown_session_is_404(
    client: TestClient, workspace_root: Path, claude_home: Path
) -> None:
    r = client.delete("/v1/sessions/never-used", headers=AUTH)

    assert r.status_code == 404
    assert r.json() == {"code": "not_found", "message": "nothing stored for sessionKey"}


def test_delete_while_a_turn_holds_the_key_is_429(
    client: TestClient, app: FastAPI, workspace_root: Path, claude_home: Path
) -> None:
    workspace = workspace_for("busy-k", root=workspace_root)
    admission = app.state.admission
    turn = Turn(session_key="busy-k", user_id=None, admission=admission, timeout_sec=5)
    admission.reserve(turn)

    r = client.delete("/v1/sessions/busy-k", headers=AUTH)

    assert r.status_code == 429
    assert r.json()["code"] == "busy"
    assert workspace.is_dir()
    admission.release(turn)


def test_a_failed_removal_is_a_500_and_a_retry_is_safe(
    client: TestClient,
    workspace_root: Path,
    claude_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sidecar import session
    from sidecar.routes import sessions as sessions_mod

    workspace = workspace_for("k-fail", root=workspace_root)
    real_remove = session.remove_paths
    calls = 0

    def failing_once(paths: Iterable[Path]) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("read-only volume")
        return real_remove(paths)

    monkeypatch.setattr(sessions_mod, "remove_paths", failing_once)
    r = client.delete("/v1/sessions/k-fail", headers=AUTH)
    assert r.status_code == 500
    assert "read-only" not in r.text  # the exception text stays in the log

    assert client.delete("/v1/sessions/k-fail", headers=AUTH).status_code == 200
    assert not workspace.exists()


def test_delete_requires_the_bearer(client: TestClient) -> None:
    assert client.delete("/v1/sessions/k").status_code == 401
