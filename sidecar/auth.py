import hmac

from fastapi import Header

from .config import get_settings
from .errors import ApiError, ErrorCode


async def require_bearer(authorization: str | None = Header(default=None)) -> None:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ApiError(ErrorCode.UNAUTHORIZED, "missing bearer token")
    expected = get_settings().bearer_secret.get_secret_value()
    if not hmac.compare_digest(token.strip().encode(), expected.encode()):
        raise ApiError(ErrorCode.UNAUTHORIZED, "invalid bearer token")
