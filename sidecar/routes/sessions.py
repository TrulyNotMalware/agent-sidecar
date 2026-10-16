import asyncio
from pathlib import Path as FsPath
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request

from ..admission import Admission
from ..auth import require_bearer
from ..claude_runner import claude_session_files
from ..codex_runner import codex_session_files
from ..config import Settings
from ..deps import SettingsDep
from ..errors import ApiError, ErrorCode
from ..observability.logging import get_logger
from ..session import known_session_ids, remove_paths, session_ids_record, workspace_path

router = APIRouter()
log = get_logger("sidecar.sessions")


@router.delete(
    # `:path` so a sessionKey containing "/" (e.g. "team/task") can be deleted too.
    "/v1/sessions/{session_key:path}",
    dependencies=[Depends(require_bearer)],
)
async def delete_session(
    session_key: Annotated[str, Path(min_length=1, max_length=256)],
    request: Request,
    settings: SettingsDep,
) -> dict[str, str]:
    admission: Admission = request.app.state.admission
    # No turn may start on the key (429) while its files go, and none may be running.
    with admission.exclusive(session_key):
        try:
            removed = await asyncio.to_thread(_remove_session, session_key, settings)
        except OSError as exc:
            log.exception("session.delete_failed", session_key=session_key)
            raise ApiError(
                ErrorCode.INTERNAL, "could not delete every file of this session; retry"
            ) from exc
    if not removed:
        raise ApiError(ErrorCode.NOT_FOUND, "nothing stored for sessionKey")
    log.info("session.deleted", session_key=session_key, paths_removed=removed)
    return {"status": "deleted", "sessionKey": session_key}


def _remove_session(session_key: str, settings: Settings) -> int:
    """Remove the workspace, the CLI's transcripts and the session id record."""
    root = settings.workspace_root
    session_ids = known_session_ids(session_key, root=root)
    workspace = workspace_path(session_key, root=root)
    transcripts: list[FsPath] = (
        codex_session_files(session_ids)
        if settings.provider == "codex"
        else claude_session_files(workspace, session_ids)
    )
    # The record last: if a removal fails, a retry still finds the transcripts by id.
    return remove_paths([*transcripts, workspace, session_ids_record(session_key, root=root)])
