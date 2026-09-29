import hmac

from fastapi import Header, HTTPException, status

from .config import get_settings
from .errors import ErrorCode


async def require_bearer(authorization: str | None = Header(default=None)) -> None:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            {"code": ErrorCode.UNAUTHORIZED.value, "message": "missing bearer token"},
        )
    expected = get_settings().bearer_secret.get_secret_value()
    if not hmac.compare_digest(token.strip().encode(), expected.encode()):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            {"code": ErrorCode.UNAUTHORIZED.value, "message": "invalid bearer token"},
        )
