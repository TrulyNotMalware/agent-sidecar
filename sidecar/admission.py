"""Which turns may run, and the registry /cancel and shutdown find them in.

Limits: one in-flight turn per sessionKey (in both modes — /cancel addresses a turn
by its sessionKey), one per X-User-Id, and MAX_CONCURRENT overall. The route reserves
before the SSE stream opens, so `busy` is always a real HTTP 429. All three limits
are checked and taken in one synchronous step — no await in between — so two
requests can never both pass.

A reservation lasts until the turn's task has closed its runner (sidecar.turn.Turn
releases it), so a sessionKey stays busy until its CLI has exited.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from .errors import ApiError, ErrorCode
from .observability.logging import get_logger
from .observability.metrics import INFLIGHT

if TYPE_CHECKING:
    from .turn import Turn

log = get_logger("sidecar.admission")


class Admission:
    def __init__(self, max_concurrent: int) -> None:
        self._max = max_concurrent
        self._by_session: dict[str, Turn] = {}
        self._users: set[str] = set()

    @property
    def inflight(self) -> int:
        return len(self._by_session)

    def reserve(self, turn: Turn) -> None:
        """Take the turn's slots, or raise ApiError(BUSY) and take nothing.

        Called by Turn.start only: a reserved turn that never starts could not be
        stopped by /cancel or drain.
        """
        if turn.session_key in self._by_session:
            raise ApiError(ErrorCode.BUSY, f"sessionKey {turn.session_key!r} has an in-flight turn")
        if turn.user_id and turn.user_id in self._users:
            raise ApiError(ErrorCode.BUSY, f"user {turn.user_id!r} has an in-flight turn")
        if len(self._by_session) >= self._max:
            raise ApiError(ErrorCode.BUSY, "sidecar concurrency cap exceeded")
        self._by_session[turn.session_key] = turn
        if turn.user_id:
            self._users.add(turn.user_id)
        INFLIGHT.set(len(self._by_session))

    def release(self, turn: Turn) -> None:
        """Give the turn's slots back. A turn that holds no reservation is a no-op."""
        if self._by_session.get(turn.session_key) is not turn:
            return
        del self._by_session[turn.session_key]
        if turn.user_id:
            self._users.discard(turn.user_id)
        INFLIGHT.set(len(self._by_session))

    def get(self, session_key: str) -> Turn | None:
        return self._by_session.get(session_key)

    async def drain(self, *, grace_sec: float) -> int:
        """Stop every in-flight turn and wait up to grace_sec for its cleanup.

        Runs at application shutdown, after the HTTP streams have ended: what is left
        are turn tasks still closing their CLI. Turn.stop cancels a turn once, so the
        runner's cleanup can finish. Returns the count of turns force-cancelled because
        they were still running after grace_sec.
        """
        turns = list(self._by_session.values())
        for turn in turns:
            turn.stop("shutdown")
        tasks = {t.task: t.session_key for t in turns if t.task is not None and not t.task.done()}
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
