import hmac
from typing import Annotated

from fastapi import Header

from .deps import SettingsDep
from .errors import ApiError, ErrorCode


async def require_bearer(
    settings: SettingsDep, authorization: Annotated[str | None, Header()] = None
) -> None:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ApiError(ErrorCode.UNAUTHORIZED, "missing bearer token")
    expected = settings.bearer_secret.get_secret_value()
    if not hmac.compare_digest(token.strip().encode(), expected.encode()):
        raise ApiError(ErrorCode.UNAUTHORIZED, "invalid bearer token")
