from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, status

from ..auth import require_bearer
from ..errors import ApiError, ErrorCode
from ..inflight import InflightRegistry
from ..observability.logging import get_logger

router = APIRouter()
log = get_logger("sidecar.cancel")


@router.post(
    # `:path` so a sessionKey containing "/" (e.g. "team/task") can be cancelled too.
    "/v1/sessions/{session_key:path}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_bearer)],
)
async def cancel(
    session_key: Annotated[str, Path(min_length=1, max_length=256)], request: Request
) -> dict[str, str]:
    registry: InflightRegistry = request.app.state.inflight
    handle = await registry.get(session_key)
    if handle is None:
        raise ApiError(ErrorCode.NOT_FOUND, "no active turn for sessionKey")

    # The turn stops itself: the stream gets `error: cancelled` right away, and the
    # sessionKey stays busy until the CLI has actually exited.
    handle.cancel_event.set()
    log.info("cancel.requested", session_key=session_key)
    return {"status": "accepted", "sessionKey": session_key}
