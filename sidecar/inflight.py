import asyncio
from dataclasses import dataclass

from .errors import ApiError, ErrorCode


@dataclass
class InflightHandle:
    session_key: str
    user_id: str | None
    cancel_event: asyncio.Event
    task: asyncio.Task


class InflightRegistry:
    """Tracks in-flight /v1/converse turns keyed by sessionKey so the cancel route can find them."""

    def __init__(self) -> None:
        self._by_session: dict[str, InflightHandle] = {}
        self._lock = asyncio.Lock()

    async def register(self, handle: InflightHandle) -> None:
        async with self._lock:
            if handle.session_key in self._by_session:
                raise ApiError(
                    ErrorCode.BUSY,
                    f"sessionKey {handle.session_key!r} already in-flight",
                )
            self._by_session[handle.session_key] = handle

    async def unregister(self, session_key: str, handle: InflightHandle) -> None:
        async with self._lock:
            current = self._by_session.get(session_key)
            if current is handle:
                del self._by_session[session_key]

    async def get(self, session_key: str) -> InflightHandle | None:
        async with self._lock:
            return self._by_session.get(session_key)

    @property
    def active_count(self) -> int:
        return len(self._by_session)

    async def drain(self, *, grace_sec: float = 10.0) -> int:
        """Signal cancel to every in-flight turn and wait up to grace_sec for natural drain.

        Returns the count of turns that had to be force-cancelled because they
        did not exit within grace_sec. Used during application shutdown.
        """
        async with self._lock:
            handles = list(self._by_session.values())
        for h in handles:
            h.cancel_event.set()

        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace_sec
        while self.active_count > 0 and loop.time() < deadline:
            await asyncio.sleep(0.05)

        async with self._lock:
            leftover = list(self._by_session.values())
        forced = 0
        for h in leftover:
            if not h.task.done():
                h.task.cancel()
                forced += 1
        return forced
