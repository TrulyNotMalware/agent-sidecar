from fastapi import Header, HTTPException, status

from .config import get_settings
from .errors import ErrorCode


async def require_bearer(authorization: str | None = Header(default=None)) -> None:
    expected = get_settings().bearer_secret
    if not expected:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            {"code": ErrorCode.INTERNAL.value, "message": "BEARER_SECRET not configured"},
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            {"code": ErrorCode.UNAUTHORIZED.value, "message": "missing bearer token"},
        )
    token = authorization.removeprefix("Bearer ").strip()
    if token != expected:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            {"code": ErrorCode.UNAUTHORIZED.value, "message": "invalid bearer token"},
        )
