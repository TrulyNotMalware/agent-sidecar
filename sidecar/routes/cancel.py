from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, status

from ..admission import Admission
from ..auth import require_bearer
from ..errors import ApiError, ErrorCode
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
    admission: Admission = request.app.state.admission
    turn = admission.get(session_key)
    if turn is None:
        raise ApiError(ErrorCode.NOT_FOUND, "no active turn for sessionKey")

    # The stream gets `error: cancelled` right away; the sessionKey stays busy until
    # the turn's task has closed the CLI.
    turn.stop("cancelled")
    log.info("cancel.requested", session_key=session_key, target_turn_id=turn.turn_id)
    return {"status": "accepted", "sessionKey": session_key}
