from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth import require_bearer
from ..errors import ErrorCode
from ..inflight import InflightRegistry
from ..observability.logging import get_logger

router = APIRouter()
log = get_logger("sidecar.cancel")


@router.post(
    "/v1/sessions/{session_key}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_bearer)],
)
async def cancel(session_key: str, request: Request) -> dict[str, str]:
    registry: InflightRegistry = request.app.state.inflight
    handle = await registry.get(session_key)
    if handle is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": ErrorCode.NOT_FOUND.value, "message": "no active turn for sessionKey"},
        )

    # The turn stops itself: the stream gets `error: cancelled` right away, and the
    # sessionKey stays busy until the CLI has actually exited.
    handle.cancel_event.set()
    log.info("cancel.requested", session_key=session_key)
    return {"status": "accepted", "sessionKey": session_key}
