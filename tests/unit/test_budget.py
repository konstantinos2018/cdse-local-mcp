"""Quota accounting: the backstop against an agent spending a month of transfer in a loop."""

from __future__ import annotations

import asyncio

import pytest

from cdse_local_mcp.config import Settings
from cdse_local_mcp.errors import BudgetExceeded
from cdse_local_mcp.transfer.budget import MAX_CONCURRENT_CONNECTIONS, TransferBudget

MB = 1024 * 1024


def _budget(call_cap: int, session_cap: int) -> TransferBudget:
    return TransferBudget(Settings(max_call_bytes=call_cap, max_session_bytes=session_cap))


def test_a_transfer_within_both_caps_is_allowed() -> None:
    _budget(100 * MB, 500 * MB).check(50 * MB)


def test_an_oversized_call_is_refused_and_says_how_to_proceed() -> None:
    with pytest.raises(BudgetExceeded) as exc:
        _budget(100 * MB, 500 * MB).check(200 * MB)
    assert "confirm=True" in (exc.value.hint or "")
    assert "ask" in (exc.value.hint or "").lower()


def test_confirmation_lifts_the_per_call_cap() -> None:
    _budget(100 * MB, 500 * MB).check(200 * MB, confirmed=True)


def test_confirmation_does_not_lift_the_session_budget() -> None:
    """A human saying yes to one file must not unlock unlimited transfer."""
    budget = _budget(1000 * MB, 300 * MB)
    with pytest.raises(BudgetExceeded, match="session budget"):
        budget.check(400 * MB, confirmed=True)


def test_the_session_budget_shrinks_as_bytes_move() -> None:
    budget = _budget(1000 * MB, 300 * MB)
    budget.record(250 * MB)

    assert budget.spent_bytes == 250 * MB
    assert budget.remaining_bytes == 50 * MB
    with pytest.raises(BudgetExceeded):
        budget.check(100 * MB, confirmed=True)


def test_concurrency_is_capped_at_the_cdse_connection_limit() -> None:
    assert MAX_CONCURRENT_CONNECTIONS == 4

    budget = _budget(MB, MB)
    peak = 0
    active = 0

    async def transfer() -> None:
        nonlocal peak, active
        async with budget.slot():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    async def run() -> None:
        await asyncio.gather(*(transfer() for _ in range(12)))

    asyncio.run(run())
    assert peak <= MAX_CONCURRENT_CONNECTIONS


def test_the_slot_is_released_when_a_transfer_raises() -> None:
    budget = _budget(MB, MB)

    async def run() -> None:
        with pytest.raises(RuntimeError):
            async with budget.slot():
                raise RuntimeError("boom")
        # If the semaphore leaked, this would deadlock.
        async with asyncio.timeout(1):
            async with budget.slot():
                pass

    asyncio.run(run())
