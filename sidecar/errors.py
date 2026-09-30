from enum import StrEnum

from .observability.redaction import scrub_secrets


class ErrorCode(StrEnum):
    BAD_REQUEST = "bad_request"
    UNAUTHORIZED = "unauthorized"
    NOT_FOUND = "not_found"
    BUSY = "busy"
    TIMEOUT = "timeout"
    SDK_ERROR = "sdk_error"
    INTERNAL = "internal"
    CANCELLED = "cancelled"


_HTTP_STATUS = {
    ErrorCode.BAD_REQUEST: 400,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.BUSY: 429,
    ErrorCode.TIMEOUT: 504,
    ErrorCode.SDK_ERROR: 502,
    ErrorCode.INTERNAL: 500,
    ErrorCode.CANCELLED: 499,
}


class ApiError(Exception):
    """`message` goes on the wire; `detail` (CLI stderr, exception text) only to the log."""

    def __init__(self, code: ErrorCode, message: str, *, detail: str | None = None) -> None:
        self.code = code
        self.message = message
        self.detail = detail
        super().__init__(message)

    @property
    def status_code(self) -> int:
        return _HTTP_STATUS[self.code]


_MAX_PROVIDER_MESSAGE = 500


def provider_message(text: str) -> str:
    """An error the provider reported (API status, quota, context length), fit for the
    wire: credentials scrubbed, length bounded. Unlike CLI stderr or exception text,
    it tells the client what went wrong without exposing the sidecar's internals."""
    text = scrub_secrets(text.strip())
    if len(text) > _MAX_PROVIDER_MESSAGE:
        text = text[: _MAX_PROVIDER_MESSAGE - 1] + "…"
    return text
