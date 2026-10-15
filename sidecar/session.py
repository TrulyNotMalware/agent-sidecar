import asyncio
import hashlib
import re
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path

from .models import SESSION_ID_PATTERN


def _digest(session_key: str) -> str:
    return hashlib.sha256(session_key.encode("utf-8")).hexdigest()


def workspace_path(session_key: str, *, root: Path) -> Path:
    digest = _digest(session_key)
    return root / digest[:2] / digest[2:34]


def workspace_for(session_key: str, *, root: Path) -> Path:
    path = workspace_path(session_key, root=root)
    path.mkdir(parents=True, exist_ok=True)
    return path


# Session ids a sessionKey was issued, kept next to (not inside) the workspaces: the
# agent's tools can write into its own workspace, so a record there could be forged.
_SESSION_IDS_DIR = ".session-ids"


def session_ids_record(session_key: str, *, root: Path) -> Path:
    return root / _SESSION_IDS_DIR / _digest(session_key)


def known_session_ids(session_key: str, *, root: Path) -> set[str]:
    try:
        text = session_ids_record(session_key, root=root).read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    return {line.strip().lower() for line in text.splitlines() if line.strip()}


def remember_session_id(session_key: str, session_id: str, *, root: Path) -> None:
    """Record an id issued to `session_key`. Not atomic across writers, which is fine:
    Admission allows one in-flight turn per sessionKey."""
    if session_id.lower() in known_session_ids(session_key, root=root):
        return
    record = session_ids_record(session_key, root=root)
    record.parent.mkdir(parents=True, exist_ok=True)
    with record.open("a", encoding="utf-8") as f:
        f.write(session_id.lower() + "\n")


def is_session_id(value: str) -> bool:
    """A UUID as both CLIs issue them: safe to put in a glob pattern or a file name."""
    return re.fullmatch(SESSION_ID_PATTERN, value) is not None


def remove_paths(paths: Iterable[Path]) -> int:
    """Remove each file or directory tree that exists; return how many did.

    Raises OSError if one cannot be removed (the rest of the list is not attempted):
    the caller reports it, and removing again is safe.
    """
    removed = 0
    for path in paths:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink(missing_ok=True)
        else:
            continue
        removed += 1
    return removed


@asynccontextmanager
async def stateless_workspace(*, parent: Path | None = None) -> AsyncIterator[Path]:
    """Create a fresh workspace directory and remove it on exit.

    Used for `mode=stateless` requests where session continuity is not desired
    and the workspace must not leak across calls. The removal runs in a thread:
    the agent may have filled the directory, and the event loop must not wait.
    """
    # Created synchronously (a mkdir or two): a cancellation can then not land between
    # creating the directory and the try/finally that removes it. Only the removal goes
    # to a thread — the agent may have filled the directory.
    path = _make_stateless_dir(parent)
    try:
        yield path
    finally:
        await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)


def _make_stateless_dir(parent: Path | None) -> Path:
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="claude-sidecar-stateless-", dir=parent))
