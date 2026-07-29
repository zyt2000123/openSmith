"""Deterministic incident detection derived from durable run observability."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from .summary_store import RunSummaryRecord


_ERROR_KINDS = frozenset({"internal", "provider_http", "provider_protocol", "provider_transport"})
_ERROR_STAGES = frozenset({"runtime_prepare", "agent_execution"})
_ERROR_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}\Z")
_PROVIDERS = frozenset({"anthropic", "gemini", "openai"})
_LEGACY_LLM_FAILURE = re.compile(
    r"⚠️ 执行失败：(?P<type>[A-Za-z_][A-Za-z0-9_]{0,99})（详情见服务端日志）\Z"
)


@dataclass(frozen=True)
class RunIncident:
    """One actionable signal associated with a completed Agent run."""

    run_id: str
    agent_id: str
    severity: str
    category: str
    message: str
    reason: str | None
    occurred_at: str
    evidence: dict[str, int | str]


class IncidentDetector:
    """Classify common run failures from summaries and redacted trace events."""

    def detect(
        self,
        record: RunSummaryRecord,
        trace: Iterable[dict[str, Any]],
    ) -> list[RunIncident]:
        trace_events = list(trace)
        incidents: list[RunIncident] = []
        summary = record.summary
        base = {
            "run_id": record.metadata.run_id,
            "agent_id": record.metadata.agent_id,
            "occurred_at": record.finished_at,
        }
        reason = summary.reason

        if reason in {"preflight_budget", "tool_failure_budget", "tool_call_budget"}:
            incidents.append(RunIncident(
                **base,
                severity="error",
                category="budget_exhausted",
                message="Run stopped after exhausting its execution budget.",
                reason=reason,
                evidence={"tool_calls": summary.tool_call_count},
            ))
        elif summary.outcome == "failed":
            failure_details = _failure_details(trace_events)
            incidents.append(RunIncident(
                **base,
                severity="error",
                category="run_failed",
                message=_failure_message(failure_details),
                reason=reason,
                evidence={"event_count": summary.event_count, **failure_details},
            ))
        elif summary.outcome in {"cancelled", "incomplete", "blocked"}:
            incidents.append(RunIncident(
                **base,
                severity="warning",
                category=f"run_{summary.outcome}",
                message=f"Run finished as {summary.outcome}.",
                reason=reason,
                evidence={"event_count": summary.event_count},
            ))

        if summary.backtrack_count >= 2:
            incidents.append(RunIncident(
                **base,
                severity="warning",
                category="repeated_backtracks",
                message="Run backtracked repeatedly and may need a routing or skill adjustment.",
                reason=None,
                evidence={"backtrack_count": summary.backtrack_count},
            ))

        timeouts = sum(
            1
            for event in trace_events
            if event.get("type") == "tool_call_result"
            and isinstance(event.get("data"), dict)
            and _is_timeout(event["data"])
        )
        if timeouts:
            incidents.append(RunIncident(
                **base,
                severity="error",
                category="tool_timeout",
                message="One or more tool calls timed out.",
                reason=None,
                evidence={"timeout_count": timeouts},
            ))
        return incidents


def _is_timeout(data: dict[str, Any]) -> bool:
    status = str(data.get("status") or "").lower()
    reason = str(data.get("reason") or data.get("error") or "").lower()
    return status == "timeout" or "timeout" in reason or "timed out" in reason


def _failure_details(trace: list[dict[str, Any]]) -> dict[str, int | str]:
    """Return only the safe failure classification retained by the runtime.

    Some traces predate the structured ``error`` payload.  Their fixed terminal
    notice still identifies the exception class, so recognize it only when it
    directly precedes the terminal failure rather than parsing arbitrary model
    output or exception text.
    """
    for event in reversed(trace):
        if event.get("type") not in {"run_finished", "failed"}:
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        details = _safe_error_details(data.get("error"))
        if details:
            return details

    for index in range(len(trace) - 1, 0, -1):
        event = trace[index]
        if event.get("type") != "failed":
            continue
        failed_data = event.get("data")
        if not isinstance(failed_data, dict) or failed_data.get("reason") != "execution_error":
            continue
        previous = trace[index - 1]
        if previous.get("type") != "text_delta":
            continue
        data = previous.get("data")
        if not isinstance(data, dict):
            continue
        text = data.get("text")
        if not isinstance(text, str):
            continue
        match = _LEGACY_LLM_FAILURE.fullmatch(text)
        if match is not None:
            return {"type": match.group("type")}
    return {}


def _safe_error_details(value: object) -> dict[str, int | str]:
    """Reduce a persisted error payload to the bounded public classification."""
    if not isinstance(value, dict):
        return {}

    details: dict[str, int | str] = {}
    kind = value.get("kind")
    if isinstance(kind, str) and kind in _ERROR_KINDS:
        details["kind"] = kind

    stage = value.get("stage")
    if isinstance(stage, str) and stage in _ERROR_STAGES:
        details["stage"] = stage

    error_type = value.get("type")
    if isinstance(error_type, str) and _ERROR_TYPE.fullmatch(error_type):
        details["type"] = error_type

    provider = value.get("provider")
    if isinstance(provider, str) and provider in _PROVIDERS:
        details["provider"] = provider

    http_status = value.get("http_status")
    if (
        isinstance(http_status, int)
        and not isinstance(http_status, bool)
        and 100 <= http_status <= 599
    ):
        details["http_status"] = http_status

    if isinstance(value.get("retryable"), bool):
        details["retryable"] = str(value["retryable"]).lower()
    return details


def _failure_message(details: dict[str, int | str]) -> str:
    """Describe the known boundary without exposing raw exception messages."""
    kind = details.get("kind")
    if kind == "provider_http":
        status = details.get("http_status")
        return (
            f"LLM provider request failed with HTTP {status}."
            if isinstance(status, int)
            else "LLM provider request failed with an HTTP error."
        )
    if kind == "provider_transport":
        return "LLM provider transport request failed."
    if kind == "provider_protocol":
        return "LLM provider returned an invalid response."
    if details.get("type") == "LLMResponseError":
        return "LLM request failed, but this trace did not retain the provider error classification."
    if details:
        return "Run failed with a retained error classification; inspect the trace evidence."
    return "Run failed without a retained error classification."
