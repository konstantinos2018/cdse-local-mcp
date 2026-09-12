"""The single chokepoint for every byte this server moves.

CDSE allows **4 concurrent connections** and roughly 12 TB per rolling 30 days. Neither limit
is visible to a language model, and an agent in a loop will find them the hard way. So every
byte-moving request acquires the one semaphore here, and every transfer is counted against a
session budget.

There must be exactly one :class:`TransferBudget` per process. Do not open an ad-hoc
``httpx.stream()`` or ``boto3`` transfer anywhere else in the codebase.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from cdse_local_mcp.config import Settings
from cdse_local_mcp.errors import BudgetExceeded

logger = logging.getLogger(__name__)

# CDSE's documented ceiling. Raising this does not make downloads faster; it makes them fail.
MAX_CONCURRENT_CONNECTIONS = 4


def _gib(size: int) -> str:
    return f"{size / 1024**3:.2f} GiB"


class TransferBudget:
    """Caps concurrency and total bytes for one server session."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_CONNECTIONS)
        self._spent = 0

    @property
    def spent_bytes(self) -> int:
        """Bytes transferred so far this session."""
        return self._spent

    @property
    def remaining_bytes(self) -> int:
        """Bytes still allowed this session."""
        return max(self._settings.max_session_bytes - self._spent, 0)

    def check(self, estimated_bytes: int, *, confirmed: bool = False) -> None:
        """Refuse a transfer that would breach either cap.

        ``confirmed`` lifts only the per-call cap, and only when a human has actually been
        asked. The session budget is never liftable at runtime — raising it is a deliberate
        act of configuration.
        """
        per_call = self._settings.max_call_bytes
        if estimated_bytes > per_call and not confirmed:
            raise BudgetExceeded(
                f"This transfer is about {_gib(estimated_bytes)}, over the "
                f"{_gib(per_call)} per-call cap.",
                hint=(
                    "Tell the user the size and ask whether to proceed. If they agree, call "
                    "again with confirm=True. Otherwise request fewer or smaller assets."
                ),
            )

        if estimated_bytes > self.remaining_bytes:
            raise BudgetExceeded(
                f"This transfer is about {_gib(estimated_bytes)} but only "
                f"{_gib(self.remaining_bytes)} of the "
                f"{_gib(self._settings.max_session_bytes)} session budget is left.",
                hint=(
                    "Raise CDSE_MAX_SESSION_BYTES and restart the server if the user really "
                    "wants more, or download fewer products."
                ),
            )

    def record(self, transferred_bytes: int) -> None:
        """Count bytes that actually moved."""
        self._spent += max(transferred_bytes, 0)
        logger.debug(
            "session transfer total %s of %s",
            _gib(self._spent),
            _gib(self._settings.max_session_bytes),
        )

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold one of the four allowed concurrent connections."""
        await self._semaphore.acquire()
        try:
            yield
        finally:
            self._semaphore.release()


_budget: TransferBudget | None = None


def get_budget(settings: Settings) -> TransferBudget:
    """Return the process-wide budget, creating it on first use."""
    global _budget
    if _budget is None:
        _budget = TransferBudget(settings)
    return _budget


def reset_budget() -> None:
    """Drop the process-wide budget. For tests."""
    global _budget
    _budget = None
