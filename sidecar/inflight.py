import asyncio
from dataclasses import dataclass

from .errors import ApiError, ErrorCode
from .observability.logging import get_logger

log = get_logger("sidecar.inflight")


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
        """Stop every in-flight turn and wait up to grace_sec for its cleanup.

        Runs at application shutdown, after the HTTP streams have ended: what is
        left are turn tasks still closing their CLI. Setting cancel_event makes a
        turn stop itself (a single cancel, so the runner's cleanup can finish).
        Returns the count of turns force-cancelled because they were still running
        after grace_sec.
        """
        async with self._lock:
            handles = list(self._by_session.values())
        for h in handles:
            h.cancel_event.set()

        tasks = {h.task: h.session_key for h in handles if not h.task.done()}
        if not tasks:
            return 0
        _, pending = await asyncio.wait(tasks, timeout=grace_sec)
        if pending:
            # A second cancel interrupts the runner's cleanup, so these CLIs may
            # outlive the process (in a container, PID 1 exiting takes them down).
            log.warning(
                "shutdown.forced_cancel",
                session_keys=sorted(tasks[t] for t in pending),
                grace_sec=grace_sec,
            )
        for task in pending:
            task.cancel()
        return len(pending)
