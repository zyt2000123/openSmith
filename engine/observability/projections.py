"""In-memory projections derived from one run's event stream."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Mapping

from engine.execution.events import EventType, ExecutionEvent


@dataclass(frozen=True)
class RunSummary:
    """A compact, safe-to-expose summary of one execution attempt."""

    run_id: str
    event_count: int
    event_counts: Mapping[str, int]
    tool_call_count: int
    backtrack_count: int
    approval_required_count: int
    token_usage: Mapping[str, int]
    outcome: str | None
    reason: str | None


@dataclass
class RunSummaryProjection:
    """Project events into a stable summary without retaining raw payloads."""

    run_id: str
    _event_counts: Counter[str] = field(default_factory=Counter)
    _tool_call_count: int = 0
    _backtrack_count: int = 0
    _approval_required_count: int = 0
    _token_usage: Counter[str] = field(default_factory=Counter)
    _outcome: str | None = None
    _reason: str | None = None

    def record(self, event: ExecutionEvent) -> None:
        """Apply one event to the projection."""
        self._event_counts[event.type.value] += 1
        if event.type is EventType.TOOL_CALL_START:
            self._tool_call_count += 1
        elif event.type is EventType.BACKTRACK:
            self._backtrack_count += 1
        elif event.type is EventType.TOOL_CALL_RESULT and event.data.get("approval_required"):
            self._approval_required_count += 1
        elif event.type is EventType.TOKEN_USAGE:
            for key in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
            ):
                value = event.data.get(key)
                if isinstance(value, int) and value >= 0:
                    self._token_usage[key] += value
        elif event.type is EventType.RUN_FINISHED:
            status = event.data.get("status")
            self._outcome = str(status) if status is not None else None
            reason = event.data.get("reason")
            self._reason = str(reason) if reason is not None else None

    def snapshot(self) -> RunSummary:
        """Return a detached summary suitable for a future query layer."""
        return RunSummary(
            run_id=self.run_id,
            event_count=sum(self._event_counts.values()),
            event_counts=dict(self._event_counts),
            tool_call_count=self._tool_call_count,
            backtrack_count=self._backtrack_count,
            approval_required_count=self._approval_required_count,
            token_usage=dict(self._token_usage),
            outcome=self._outcome,
            reason=self._reason,
        )
