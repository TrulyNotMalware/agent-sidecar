from enum import StrEnum


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


def http_status_for(code: ErrorCode) -> int:
    return _HTTP_STATUS[code]


class ApiError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)

    @property
    def status_code(self) -> int:
        return http_status_for(self.code)
