"""Minimal cron expression parser for common scheduling patterns.

Supports standard 5-field cron: minute hour day_of_month month day_of_week
Each field is a comma-separated list of terms, where a term is ``*``, a single
value, or an ``a-b`` range, any of which may carry a ``/step``.  Months accept
``JAN``-``DEC`` and weekdays ``SUN``-``SAT``; weekday 0 and 7 both mean Sunday.
Out-of-range values are rejected rather than silently never matching.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


_WEEKDAY_NAMES = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}
_MONTH_NAMES = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_value(text: str, min_val: int, max_val: int, names: dict[str, int]) -> int:
    """Resolve one literal — a number or a three-letter name — inside its field's range."""
    token = text.strip()
    value = names.get(token.upper())
    if value is None:
        try:
            value = int(token)
        except ValueError:
            raise ValueError(f"cron value {text!r} is not a number or a known name") from None
    if not min_val <= value <= max_val:
        raise ValueError(f"cron value {value} is out of range {min_val}-{max_val}")
    return value


def _parse_term(term: str, min_val: int, max_val: int, names: dict[str, int]) -> range:
    """Parse one comma-separated term: ``*``, ``a`` or ``a-b``, each optionally ``/step``."""
    base, slash, step_text = term.partition("/")
    step = 1
    if slash:
        try:
            step = int(step_text.strip())
        except ValueError:
            raise ValueError(f"cron step {step_text!r} is not a number") from None
        if step <= 0:
            raise ValueError(f"cron step must be positive, got {step}")

    base = base.strip()
    if base == "*":
        return range(min_val, max_val + 1, step)

    low_text, dash, high_text = base.partition("-")
    if not dash:
        value = _parse_value(base, min_val, max_val, names)
        if slash:
            # Vixie cron reads "5/15" as "5-max/15". Accepting it here without that
            # meaning would silently drop the step and schedule a single value the
            # user did not ask for, so name the problem instead.
            raise ValueError(f"cron step needs a range or '*', not the single value {base!r}")
        return range(value, value + 1)

    start = _parse_value(low_text, min_val, max_val, names)
    end = _parse_value(high_text, min_val, max_val, names)
    if start > end:
        raise ValueError(f"cron range {base!r} runs backwards")
    return range(start, end + 1, step)


def _parse_field(
    field: str,
    min_val: int,
    max_val: int,
    names: dict[str, int] | None = None,
) -> list[int]:
    """Parse a cron field into the sorted set of values it matches."""
    values: set[int] = set()
    for term in field.split(","):
        if not term.strip():
            raise ValueError(f"cron field {field!r} has an empty term")
        values.update(_parse_term(term, min_val, max_val, names or {}))
    return sorted(values)


def next_cron_time(expression: str, after: datetime | None = None) -> datetime:
    """Calculate the next run time for a 5-field cron expression.

    Args:
        expression: "minute hour day month weekday" (e.g. "*/5 * * * *")
        after: start time (defaults to now UTC)

    Returns:
        Next datetime (UTC) when the cron should fire.
    """
    if after is None:
        after = datetime.now(timezone.utc)

    parts = expression.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Cron expression must have 5 fields, got {len(parts)}: {expression!r}")

    minutes = _parse_field(parts[0], 0, 59)
    hours = _parse_field(parts[1], 0, 23)
    days = _parse_field(parts[2], 1, 31)
    months = _parse_field(parts[3], 1, 12, _MONTH_NAMES)
    # Upper bound 7, not 6: cron accepts both 0 and 7 for Sunday, and the range
    # check added below would otherwise reject a legal "* * * * 7".
    weekdays = _parse_field(parts[4], 0, 7, _WEEKDAY_NAMES)

    # Convert cron weekday (0=Sun) to Python weekday (0=Mon)
    py_weekdays = [(d - 1) % 7 for d in weekdays] if parts[4] != "*" else None
    day_of_month_restricted = parts[2] != "*"
    day_of_week_restricted = py_weekdays is not None

    # Start searching from the next minute
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)

    # A 5-field expression can legitimately wait across a skipped century leap
    # year (for example, 29 February from 2096 to 2104), so cover the longest
    # Gregorian leap-day gap while still bounding unsatisfiable expressions.
    limit = after + timedelta(days=366 * 8)

    while candidate < limit:
        if candidate.month not in months:
            # Skip to first day of next valid month
            candidate = candidate.replace(day=1, hour=0, minute=0) + timedelta(days=32)
            candidate = candidate.replace(day=1, hour=0, minute=0)
            continue

        day_of_month_matches = candidate.day in days
        day_of_week_matches = (
            candidate.weekday() in py_weekdays
            if py_weekdays is not None
            else True
        )
        if day_of_month_restricted and day_of_week_restricted:
            day_matches = day_of_month_matches or day_of_week_matches
        else:
            day_matches = day_of_month_matches and day_of_week_matches
        if not day_matches:
            candidate = candidate.replace(hour=0, minute=0) + timedelta(days=1)
            continue

        if candidate.hour not in hours:
            candidate = candidate.replace(minute=0) + timedelta(hours=1)
            continue

        if candidate.minute not in minutes:
            candidate += timedelta(minutes=1)
            continue

        return candidate

    raise ValueError(f"No valid next run time found within eight years for: {expression!r}")


def next_interval_time(seconds: int, after: datetime | None = None) -> datetime:
    """Calculate the next run time for an interval-based trigger.

    Args:
        seconds: interval in seconds
        after: start time (defaults to now UTC)
    """
    if after is None:
        after = datetime.now(timezone.utc)
    if seconds <= 0:
        raise ValueError("Interval must be positive")
    return after + timedelta(seconds=seconds)
