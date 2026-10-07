import asyncio
import hashlib
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path


def _digest(session_key: str) -> str:
    return hashlib.sha256(session_key.encode("utf-8")).hexdigest()


def workspace_for(session_key: str, *, root: Path) -> Path:
    digest = _digest(session_key)
    path = root / digest[:2] / digest[2:34]
    path.mkdir(parents=True, exist_ok=True)
    return path


# Session ids a sessionKey was issued, kept next to (not inside) the workspaces: the
# agent's tools can write into its own workspace, so a record there could be forged.
_SESSION_IDS_DIR = ".session-ids"


def known_session_ids(session_key: str, *, root: Path) -> set[str]:
    try:
        text = (root / _SESSION_IDS_DIR / _digest(session_key)).read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    return {line.strip().lower() for line in text.splitlines() if line.strip()}


def remember_session_id(session_key: str, session_id: str, *, root: Path) -> None:
    """Record an id issued to `session_key`. Not atomic across writers, which is fine:
    Admission allows one in-flight turn per sessionKey."""
    if session_id.lower() in known_session_ids(session_key, root=root):
        return
    directory = root / _SESSION_IDS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / _digest(session_key)).open("a", encoding="utf-8") as f:
        f.write(session_id.lower() + "\n")


@asynccontextmanager
async def stateless_workspace(*, parent: Path | None = None) -> AsyncIterator[Path]:
    """Create a fresh workspace directory and remove it on exit.

    Used for `mode=stateless` requests where session continuity is not desired
    and the workspace must not leak across calls. The removal runs in a thread:
    the agent may have filled the directory, and the event loop must not wait.
    """
    path = await asyncio.to_thread(_make_stateless_dir, parent)
    try:
        yield path
    finally:
        await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)


def _make_stateless_dir(parent: Path | None) -> Path:
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="claude-sidecar-stateless-", dir=parent))
