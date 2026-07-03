import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from .errors import ApiError, ErrorCode


class ConcurrencyGate:
    def __init__(self, max_concurrent: int) -> None:
        self._max = max_concurrent
        self._inflight = 0
        self._user_inflight: set[str] = set()
        self._session_inflight: set[str] = set()
        self._lock = asyncio.Lock()

    @property
    def inflight(self) -> int:
        return self._inflight

    def _ensure_capacity(self, *, user_id: str | None, session_key: str | None) -> None:
        if self._inflight >= self._max:
            raise ApiError(ErrorCode.BUSY, "sidecar concurrency cap exceeded")
        if user_id and user_id in self._user_inflight:
            raise ApiError(ErrorCode.BUSY, f"user {user_id!r} has an in-flight turn")
        if session_key and session_key in self._session_inflight:
            raise ApiError(
                ErrorCode.BUSY,
                f"sessionKey {session_key!r} has an in-flight turn",
            )

    async def check(
        self,
        *,
        user_id: str | None = None,
        session_key: str | None = None,
    ) -> None:
        """Raise BUSY if acquire() would currently be rejected. Reserves nothing."""
        async with self._lock:
            self._ensure_capacity(user_id=user_id, session_key=session_key)

    @asynccontextmanager
    async def acquire(
        self,
        *,
        user_id: str | None = None,
        session_key: str | None = None,
    ) -> AsyncIterator[None]:
        async with self._lock:
            self._ensure_capacity(user_id=user_id, session_key=session_key)
            self._inflight += 1
            if user_id:
                self._user_inflight.add(user_id)
            if session_key:
                self._session_inflight.add(session_key)
        try:
            yield
        finally:
            async with self._lock:
                self._inflight -= 1
                if user_id:
                    self._user_inflight.discard(user_id)
                if session_key:
                    self._session_inflight.discard(session_key)
