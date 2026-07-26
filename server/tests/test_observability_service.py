from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from app.services.observability_service import ObservabilityService
from engine.execution import EventType, ExecutionEvent, RunObservationContext
from engine.observability import (
    ObservabilityReader,
    RunObservation,
)


def _service_with_run(tmp_path: Path) -> ObservabilityService:
    observation = RunObservation.start(RunObservationContext(
        run_id="run-1",
        agent_id="smith-id",
        session_id="session-1",
        profile_dir=tmp_path,
        created_at="2026-07-19T00:00:00+00:00",
    ))
    observation.record(ExecutionEvent(EventType.TOOL_CALL_START, {"name": "shell"}))
    observation.record(ExecutionEvent(EventType.TOKEN_USAGE, {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }))
    observation.record(ExecutionEvent(EventType.RUN_FINISHED, {"status": "completed"}))
    return ObservabilityService(ObservabilityReader(tmp_path))


def test_observability_service_lists_owned_summaries_and_trace(tmp_path: Path) -> None:
    service = _service_with_run(tmp_path)

    runs = service.list_runs("smith-id", limit=10)
    summary = service.get_run("smith-id", "run-1")
    trace = service.get_trace("smith-id", "run-1", limit=10)

    assert [run.run_id for run in runs] == ["run-1"]
    assert summary.outcome == "completed"
    assert summary.tool_call_count == 1
    assert summary.total_tokens == 15
    assert [event.type for event in trace] == ["tool_call_start", "token_usage", "run_finished"]

    health = service.get_health("smith-id", limit=10)

    assert health.success_rate == 1.0
    assert health.tool_call_count == 1
    assert health.tool_success_rate is None
    assert health.tokens_per_run == 15.0


def test_observability_service_derives_tool_timeout_incidents(tmp_path: Path) -> None:
    observation = RunObservation.start(RunObservationContext(
        run_id="run-timeout", agent_id="smith-id", profile_dir=tmp_path,
        created_at="2026-07-19T00:00:00+00:00",
    ))
    observation.record(ExecutionEvent(EventType.TOOL_CALL_RESULT, {
        "name": "shell", "status": "timeout", "reason": "command timed out",
    }))
    observation.record(ExecutionEvent(EventType.RUN_FINISHED, {"status": "failed", "reason": "tool_failure_budget"}))
    service = ObservabilityService(ObservabilityReader(tmp_path))

    incidents = service.list_incidents("smith-id", limit=10)

    assert [(incident.category, incident.severity) for incident in incidents] == [
        ("budget_exhausted", "error"),
        ("tool_timeout", "error"),
    ]

    diagnosis = service.get_diagnosis("smith-id", "run-timeout")

    assert diagnosis.failure_node == "tool:shell"
    assert diagnosis.primary_category == "tool_timeout"
    assert diagnosis.evidence == ["timeout_count=1", "tool=shell"]
    assert diagnosis.recommendation is not None

    proposal = service.get_improvement_proposal("smith-id", "run-timeout")

    assert proposal.status == "proposed"
    assert proposal.category == "tool_timeout"
    assert proposal.approval_required is True


def test_observability_service_does_not_expose_another_agents_run(tmp_path: Path) -> None:
    service = _service_with_run(tmp_path)

    assert service.list_runs("another-agent", limit=10) == []
    with pytest.raises(HTTPException) as exc:
        service.get_trace("another-agent", "run-1", limit=10)
    assert exc.value.status_code == 404
