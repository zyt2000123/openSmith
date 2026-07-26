from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.utils.cron import next_cron_time, next_interval_time  # noqa: E402

# A fixed Sunday, so every expectation below is stable rather than clock-dependent.
AFTER = datetime(2026, 7, 26, 10, 30, tzinfo=timezone.utc)


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        # Syntax that already worked; kept so the parser rewrite cannot drop it.
        ("* * * * *", _utc(2026, 7, 26, 10, 31)),
        ("*/5 * * * *", _utc(2026, 7, 26, 10, 35)),
        ("15 * * * *", _utc(2026, 7, 26, 11, 15)),
        ("0 9,18 * * *", _utc(2026, 7, 26, 18, 0)),
        # Ranges: "weekdays at 09:00" is the expression this parser used to reject.
        ("0 9 * * 1-5", _utc(2026, 7, 27, 9, 0)),
        ("0 0 1-3,5 * *", _utc(2026, 8, 1, 0, 0)),
        # Range with a step.
        ("30 8-18/2 * * *", _utc(2026, 7, 26, 12, 30)),
        # Three-letter names.
        ("0 0 * * MON", _utc(2026, 7, 27, 0, 0)),
        ("0 0 1 JAN *", _utc(2027, 1, 1, 0, 0)),
    ],
)
def test_next_cron_time_resolves_supported_syntax(expression: str, expected: datetime) -> None:
    assert next_cron_time(expression, after=AFTER) == expected


def test_weekday_zero_and_seven_both_mean_sunday() -> None:
    """Range checking must not reject the legal upper bound of the weekday field."""
    assert next_cron_time("0 12 * * 0", after=AFTER) == _utc(2026, 7, 26, 12, 0)
    assert next_cron_time("0 12 * * 7", after=AFTER) == next_cron_time("0 12 * * 0", after=AFTER)


@pytest.mark.parametrize(
    ("expression", "reason"),
    [
        ("not a cron", "must have 5 fields"),
        ("* * * * * *", "must have 5 fields"),
        ("99 * * * *", "out of range"),
        ("0 0 * * 8", "out of range"),
        ("0 0 * * FOO", "not a number or a known name"),
        ("*/0 * * * *", "step must be positive"),
        ("*/x * * * *", "is not a number"),
        ("5/15 * * * *", "step needs a range"),
        ("5-1 * * * *", "runs backwards"),
        ("1-2-3 * * * *", "not a number or a known name"),
        ("1,,2 * * * *", "empty term"),
        # Syntactically fine but can never fire: February has no 30th.
        ("0 0 30 2 *", "No valid next run time"),
    ],
)
def test_next_cron_time_rejects_with_a_reason(expression: str, reason: str) -> None:
    """Every rejection names its cause: the caller turns this text into a 422 body."""
    with pytest.raises(ValueError, match=reason):
        next_cron_time(expression, after=AFTER)


def test_next_interval_time_requires_a_positive_interval() -> None:
    assert next_interval_time(3600, after=AFTER) == _utc(2026, 7, 26, 11, 30)
    with pytest.raises(ValueError, match="must be positive"):
        next_interval_time(0, after=AFTER)
