from pathlib import Path

from sidecar.session import stateless_workspace, workspace_for


def test_workspace_for_is_deterministic(tmp_path: Path) -> None:
    assert workspace_for("k1", root=tmp_path) == workspace_for("k1", root=tmp_path)


def test_workspace_for_distinguishes_keys(tmp_path: Path) -> None:
    assert workspace_for("k1", root=tmp_path) != workspace_for("k2", root=tmp_path)


def test_workspace_for_creates_directory(tmp_path: Path) -> None:
    p = workspace_for("hello", root=tmp_path)
    assert p.is_dir()


async def test_stateless_workspace_creates_and_cleans_up(tmp_path: Path) -> None:
    captured: Path
    async with stateless_workspace(parent=tmp_path) as ws:
        assert ws.is_dir()
        captured = ws
        (ws / "scratch.txt").write_text("data")
    assert not captured.exists()


async def test_stateless_workspace_unique_per_call(tmp_path: Path) -> None:
    seen: list[Path] = []
    async with (
        stateless_workspace(parent=tmp_path) as a,
        stateless_workspace(parent=tmp_path) as b,
    ):
        assert a != b
        seen = [a, b]
    for p in seen:
        assert not p.exists()


def test_session_ids_are_remembered_per_session_key(tmp_path: Path) -> None:
    from sidecar.session import known_session_ids, remember_session_id

    remember_session_id("k1", "AAAA-1", root=tmp_path)
    remember_session_id("k1", "aaaa-1", root=tmp_path)  # same id, different case
    remember_session_id("k1", "bbbb-2", root=tmp_path)

    assert known_session_ids("k1", root=tmp_path) == {"aaaa-1", "bbbb-2"}
    assert known_session_ids("k2", root=tmp_path) == set()


def test_session_id_record_lives_outside_the_workspace(tmp_path: Path) -> None:
    # The agent can write inside its workspace; the record must not be forgeable there.
    from sidecar.session import remember_session_id

    ws = workspace_for("k1", root=tmp_path)
    remember_session_id("k1", "aaaa-1", root=tmp_path)

    assert not any(p.is_file() for p in ws.rglob("*"))


def test_workspace_path_does_not_create_the_workspace(tmp_path: Path) -> None:
    from sidecar.session import workspace_path

    path = workspace_path("k1", root=tmp_path)

    assert not path.exists()
    assert workspace_for("k1", root=tmp_path) == path


def test_remove_paths_counts_what_existed(tmp_path: Path) -> None:
    from sidecar.session import remove_paths

    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "f").write_text("x")
    file = tmp_path / "file"
    file.write_text("x")
    link = tmp_path / "link"
    link.symlink_to(tree)  # a link is removed, not what it points to

    assert remove_paths([link, tree, file, tmp_path / "missing"]) == 3
    assert list(tmp_path.iterdir()) == []


def test_only_uuids_count_as_session_ids() -> None:
    from sidecar.session import is_session_id

    assert is_session_id("01a0ec1c-14d2-7d12-aaa7-47100d58f161")
    assert not is_session_id("sess-fake-1")
    assert not is_session_id("*")
    assert not is_session_id("../01a0ec1c-14d2-7d12-aaa7-47100d58f161")
