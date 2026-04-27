import asyncio
import contextlib

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth import require_bearer
from ..config import get_settings
from ..errors import ErrorCode
from ..inflight import InflightHandle, InflightRegistry
from ..observability.logging import get_logger

router = APIRouter()
log = get_logger("sidecar.cancel")

_BACKGROUND: set[asyncio.Task] = set()


@router.post(
    "/v1/sessions/{session_key}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_bearer)],
)
async def cancel(session_key: str, request: Request) -> dict[str, str]:
    settings = get_settings()
    registry: InflightRegistry = request.app.state.inflight
    handle = await registry.get(session_key)
    if handle is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": ErrorCode.NOT_FOUND.value, "message": "no active turn for sessionKey"},
        )

    task = asyncio.create_task(_escalate_cancel(handle, settings.cancel_grace_sec))
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)

    log.info("cancel.requested", session_key=session_key, grace_sec=settings.cancel_grace_sec)
    return {"status": "accepted", "sessionKey": session_key}


async def _escalate_cancel(handle: InflightHandle, grace_sec: float) -> None:
    handle.cancel_event.set()
    try:
        await asyncio.wait_for(asyncio.shield(_swallow(handle.task)), timeout=grace_sec)
        return
    except TimeoutError:
        pass
    if not handle.task.done():
        handle.task.cancel()


async def _swallow(task: asyncio.Task) -> None:
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await task
