"""Store operating hours.

After-hours detection is only as trustworthy as this module. Two things make
it harder than it looks:

**Timezones.** A store in Dar es Salaam opens at 08:00 EAT. The server may run
in UTC, and the events carry UTC timestamps. Comparing a UTC time against a
local opening time is a bug that produces three hours of wrong answers every
day, and it is invisible in a test suite written in UTC.

**Overnight shifts.** A bar open 20:00-04:00 has a closing time numerically
*before* its opening time. Naive comparison marks the entire trading night as
after-hours and buries the operator in alerts.

The design also fails **safe**: a day with no configured hours is treated as
closed, so an unconfigured store raises alerts rather than silently
suppressing them. Silence would look like everything is fine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: Python's ``date.weekday()``: Monday = 0 .. Sunday = 6.
WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


class ScheduleError(ValueError):
    """The operating-hours configuration is not usable."""


def resolve_timezone(name: str | None) -> ZoneInfo:
    """Look up a timezone, falling back to UTC rather than crashing.

    A misconfigured timezone must not take a camera pipeline down. Falling
    back to UTC makes after-hours detection wrong but keeps every other rule
    running, and the caller logs the substitution.
    """
    if not name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo("UTC")


@dataclass(frozen=True, slots=True)
class DayHours:
    """Opening hours for one weekday, in the store's local time."""

    day_of_week: int
    opens_at: time | None = None
    closes_at: time | None = None
    is_closed: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.day_of_week <= 6:
            raise ScheduleError("day_of_week must be 0 (Monday) to 6 (Sunday)")
        if not self.is_closed and (self.opens_at is None or self.closes_at is None):
            raise ScheduleError(
                f"{WEEKDAY_NAMES[self.day_of_week]} is open but has no opening times"
            )

    @property
    def is_overnight(self) -> bool:
        """Whether the shift runs past midnight into the next day."""
        if self.is_closed or self.opens_at is None or self.closes_at is None:
            return False
        return self.closes_at < self.opens_at

    @property
    def is_all_day(self) -> bool:
        """A 24-hour operation, expressed as 00:00-00:00."""
        return not self.is_closed and self.opens_at == time(0, 0) and self.closes_at == time(0, 0)

    def describe(self) -> str:
        if self.is_closed:
            return f"{WEEKDAY_NAMES[self.day_of_week]}: closed"
        if self.is_all_day:
            return f"{WEEKDAY_NAMES[self.day_of_week]}: open 24 hours"
        suffix = " (next day)" if self.is_overnight else ""
        return (
            f"{WEEKDAY_NAMES[self.day_of_week]}: "
            f"{self.opens_at:%H:%M}-{self.closes_at:%H:%M}{suffix}"
        )


@dataclass
class OperatingSchedule:
    """A store's weekly opening hours.

    Days with no entry are **closed**. That is the fail-safe direction: an
    unconfigured store generates after-hours alerts, which someone will notice
    and fix, rather than silently never alerting.
    """

    timezone_name: str = "UTC"
    days: dict[int, DayHours] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.days is None:
            self.days = {}
        self._zone = resolve_timezone(self.timezone_name)

    @property
    def zone(self) -> ZoneInfo:
        return self._zone

    @property
    def is_configured(self) -> bool:
        return bool(self.days)

    @classmethod
    def always_open(cls, timezone_name: str = "UTC") -> OperatingSchedule:
        """A 24/7 schedule. Useful for warehouses and for tests."""
        return cls(
            timezone_name=timezone_name,
            days={
                day: DayHours(day_of_week=day, opens_at=time(0, 0), closes_at=time(0, 0))
                for day in range(7)
            },
        )

    @classmethod
    def from_rows(cls, timezone_name: str, rows: list[dict]) -> OperatingSchedule:
        """Build from database rows of ``store_operating_hours``."""
        days: dict[int, DayHours] = {}
        for row in rows:
            hours = DayHours(
                day_of_week=int(row["day_of_week"]),
                opens_at=row.get("opens_at"),
                closes_at=row.get("closes_at"),
                is_closed=bool(row.get("is_closed", False)),
            )
            days[hours.day_of_week] = hours
        return cls(timezone_name=timezone_name, days=days)

    # ------------------------------------------------------------------ query
    def local_time(self, moment: datetime) -> datetime:
        """Convert an instant to the store's local wall-clock time."""
        if moment.tzinfo is None:
            # Naive input is interpreted as UTC: the server's timezone must
            # never leak into a business rule.
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(self._zone)

    def is_open_at(self, moment: datetime) -> bool:
        """Whether the store is trading at ``moment``.

        Handles overnight shifts by also checking the *previous* day's hours:
        at 02:00 on Tuesday, a Monday 20:00-04:00 shift is still running.
        """
        if not self.is_configured:
            return False

        local = self.local_time(moment)
        today = self.days.get(local.weekday())

        if today is not None and not today.is_closed:
            if today.is_all_day:
                return True
            assert today.opens_at is not None and today.closes_at is not None
            if today.is_overnight:
                # Opens today, closes tomorrow: open from opening until midnight.
                if local.time() >= today.opens_at:
                    return True
            elif today.opens_at <= local.time() < today.closes_at:
                return True

        # Still inside yesterday's overnight shift?
        yesterday_index = (local.weekday() - 1) % 7
        yesterday = self.days.get(yesterday_index)
        if yesterday is not None and not yesterday.is_closed and yesterday.is_overnight:
            assert yesterday.closes_at is not None
            if local.time() < yesterday.closes_at:
                return True

        return False

    def is_after_hours(self, moment: datetime) -> bool:
        """The inverse of :meth:`is_open_at`, named for how rules read."""
        return not self.is_open_at(moment)

    def next_open_change(self, moment: datetime, horizon_hours: int = 48) -> datetime | None:
        """When the open/closed state next flips.

        Used by the scheduler to decide when to re-evaluate rather than
        polling every minute. Resolution is one minute, which is finer than
        any opening time a store actually configures.
        """
        local = self.local_time(moment)
        currently_open = self.is_open_at(moment)
        cursor = local.replace(second=0, microsecond=0)
        for _ in range(horizon_hours * 60):
            cursor += timedelta(minutes=1)
            if self.is_open_at(cursor) != currently_open:
                return cursor.astimezone(UTC)
        return None

    def describe(self) -> list[str]:
        """Human-readable summary, Monday first."""
        lines = []
        for day in range(7):
            hours = self.days.get(day)
            lines.append(
                hours.describe() if hours else f"{WEEKDAY_NAMES[day]}: closed (not configured)"
            )
        return lines
