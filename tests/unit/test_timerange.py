"""Date handling. "An image for 15 July" must mean the whole day, not an instant."""

from __future__ import annotations

import pytest

from cdse_local_mcp.domain.timerange import resolve_time_range
from cdse_local_mcp.errors import InvalidRequest


def test_a_single_date_covers_the_whole_day() -> None:
    rng = resolve_time_range("2024-07-31")
    assert rng.as_stac() == "2024-07-31T00:00:00.000Z/2024-07-31T23:59:59.000Z"


def test_a_date_range_spans_both_days_fully() -> None:
    rng = resolve_time_range("2024-07-01", "2024-07-31")
    assert rng.as_stac().startswith("2024-07-01T00:00:00")
    assert rng.as_stac().endswith("2024-07-31T23:59:59.000Z")


def test_naive_instants_are_treated_as_utc() -> None:
    rng = resolve_time_range("2024-07-31T09:20:31", "2024-07-31T10:00:00")
    assert rng.as_stac() == "2024-07-31T09:20:31.000Z/2024-07-31T10:00:00.000Z"


def test_offset_instants_are_converted_to_utc() -> None:
    rng = resolve_time_range("2024-07-31T12:00:00+03:00", "2024-07-31T13:00:00+03:00")
    assert rng.as_stac().startswith("2024-07-31T09:00:00")


def test_reversed_range_is_rejected() -> None:
    with pytest.raises(InvalidRequest, match="before"):
        resolve_time_range("2024-07-31", "2024-07-01")


def test_unparseable_date_is_rejected_with_an_example() -> None:
    with pytest.raises(InvalidRequest) as exc:
        resolve_time_range("31 July 2024")
    assert "YYYY-MM-DD" in (exc.value.hint or "")


def test_empty_date_is_rejected() -> None:
    with pytest.raises(InvalidRequest, match="empty"):
        resolve_time_range("   ")
