"""Run-window health metrics derived from local observability records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .summary_store import RunSummaryRecord


@dataclass(frozen=True)
class AgentHealth:
    """Stable, aggregate health metrics for a bounded window of Agent runs."""

    agent_id: str
    run_count: int
    completed_count: int
    unsuccessful_count: int
    success_rate: float
    tool_call_count: int
    tool_success_rate: float | None
    average_backtracks: float
    total_tokens: int
    tokens_per_run: float


class HealthCalculator:
    """Compute metrics without exposing traces or storage layout to callers."""

    def calculate(
        self,
        agent_id: str,
        records: Iterable[RunSummaryRecord],
        traces: Iterable[Iterable[dict[str, Any]]],
    ) -> AgentHealth:
        runs = list(records)
        trace_events = [event for trace in traces for event in trace]
        run_count = len(runs)
        completed_count = sum(run.summary.outcome == "completed" for run in runs)
        tool_call_count = sum(run.summary.tool_call_count for run in runs)
        total_tokens = sum(int(run.summary.token_usage.get("total_tokens", 0)) for run in runs)
        # A pre-approval TOOL_CALL_RESULT (blocked=True, approval_required=True)
        # is a gate probe, not an executed tool: the real result arrives later
        # under a separate event.  Counting both would double-count one approved
        # call as a failure plus a success, so skip the gate probe entirely.
        tool_results = [
            event["data"]
            for event in trace_events
            if event.get("type") == "tool_call_result"
            and isinstance(event.get("data"), dict)
            and not event["data"].get("approval_required")
        ]
        successful_tools = sum(_tool_succeeded(result) for result in tool_results)
        return AgentHealth(
            agent_id=agent_id,
            run_count=run_count,
            completed_count=completed_count,
            unsuccessful_count=run_count - completed_count,
            success_rate=_ratio(completed_count, run_count),
            tool_call_count=tool_call_count,
            tool_success_rate=_ratio(successful_tools, len(tool_results)) if tool_results else None,
            average_backtracks=_ratio(sum(run.summary.backtrack_count for run in runs), run_count),
            total_tokens=total_tokens,
            tokens_per_run=_ratio(total_tokens, run_count),
        )


def _tool_succeeded(data: dict[str, Any]) -> bool:
    if data.get("blocked") or data.get("approval_required"):
        return False
    status = str(data.get("status") or "").lower()
    if status:
        return status in {"ok", "success", "completed", "passed"}
    return not bool(data.get("error"))


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0
