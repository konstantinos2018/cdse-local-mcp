"""Date and time-range parsing.

Everything CDSE sees is UTC ISO-8601 with an explicit ``Z``. A bare date is widened to
cover the whole day, which is almost always what "an image for 15 July" means.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from cdse_local_mcp.errors import InvalidRequest

_DATE_ONLY_LENGTH = 10


@dataclass(frozen=True)
class TimeRange:
    """A closed UTC interval, rendered as a STAC ``datetime`` range."""

    start: datetime
    end: datetime

    def as_stac(self) -> str:
        return f"{_iso(self.start)}/{_iso(self.end)}"

    def describe(self) -> str:
        return f"{_iso(self.start)} to {_iso(self.end)} (UTC)"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_one(raw: str, *, field: str, end_of_day: bool) -> datetime:
    text = raw.strip()
    if not text:
        raise InvalidRequest(f"{field} is empty.", hint="Use YYYY-MM-DD or an ISO-8601 instant.")

    if len(text) == _DATE_ONLY_LENGTH:
        try:
            day = date.fromisoformat(text)
        except ValueError as exc:
            raise InvalidRequest(
                f"Could not parse {field}={raw!r} as a date.",
                hint="Use YYYY-MM-DD, for example 2024-07-15.",
            ) from exc
        time_part = (
            datetime.max.time().replace(microsecond=0) if end_of_day else datetime.min.time()
        )
        return datetime.combine(day, time_part, tzinfo=UTC)

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidRequest(
            f"Could not parse {field}={raw!r} as a date or datetime.",
            hint="Use YYYY-MM-DD or an ISO-8601 instant such as 2024-07-15T09:20:31Z.",
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def resolve_time_range(start_date: str, end_date: str | None = None) -> TimeRange:
    """Build a UTC range from ISO dates or instants.

    A single ``start_date`` with no ``end_date`` covers that one day, so asking for
    "2024-07-15" searches 00:00:00 to 23:59:59 UTC rather than a single instant.
    """
    start = _parse_one(start_date, field="start_date", end_of_day=False)
    end = (
        _parse_one(end_date, field="end_date", end_of_day=True)
        if end_date
        else _parse_one(start_date, field="start_date", end_of_day=True)
    )

    if end < start:
        raise InvalidRequest(
            f"end_date ({_iso(end)}) is before start_date ({_iso(start)}).",
            hint="Swap them, or drop end_date to search a single day.",
        )
    return TimeRange(start=start, end=end)
