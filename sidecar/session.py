import hashlib
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def workspace_for(session_key: str, *, root: Path) -> Path:
    digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()
    path = root / digest[:2] / digest[2:34]
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def stateless_workspace(*, parent: Path | None = None) -> Iterator[Path]:
    """Create a fresh workspace directory and remove it on exit.

    Used for `mode=stateless` requests where session continuity is not desired
    and the workspace must not leak across calls.
    """
    parent_str: str | None = None
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
        parent_str = str(parent)
    path = Path(tempfile.mkdtemp(prefix="claude-sidecar-stateless-", dir=parent_str))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
