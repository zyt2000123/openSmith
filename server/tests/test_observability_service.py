from __future__ import annotations

import json
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
        "name": "shell", "error_kind": "timeout", "timed_out": True, "reason": "command timed out",
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


def test_observability_service_explains_current_and_legacy_llm_failures(tmp_path: Path) -> None:
    current = RunObservation.start(RunObservationContext(
        run_id="run-provider-http", agent_id="smith-id", profile_dir=tmp_path,
        created_at="2026-07-19T00:00:00+00:00",
    ))
    details = {
        "kind": "provider_http",
        "stage": "agent_execution",
        "type": "LLMResponseError",
        "provider": "openai",
        "http_status": 429,
        "retryable": True,
        "message": "api_key=must-not-be-exposed",
    }
    current.record(ExecutionEvent(EventType.FAILED, {
        "reason": "execution_error", "error": details,
    }))
    current.record(ExecutionEvent(EventType.RUN_FINISHED, {
        "status": "failed", "reason": "execution_error", "error": details,
    }))

    legacy = RunObservation.start(RunObservationContext(
        run_id="run-legacy-llm", agent_id="smith-id", profile_dir=tmp_path,
        created_at="2026-07-19T00:00:01+00:00",
    ))
    legacy.record(ExecutionEvent(EventType.TEXT_DELTA, {
        "text": "⚠️ 执行失败：LLMResponseError（详情见服务端日志）",
    }))
    legacy.record(ExecutionEvent(EventType.FAILED, {"reason": "execution_error"}))
    legacy.record(ExecutionEvent(EventType.RUN_FINISHED, {
        "status": "failed", "reason": "execution_error",
    }))

    service = ObservabilityService(ObservabilityReader(tmp_path))
    incidents = {incident.run_id: incident for incident in service.list_incidents("smith-id", limit=10)}

    provider_incident = incidents["run-provider-http"]
    assert provider_incident.message == "LLM provider request failed with HTTP 429."
    assert provider_incident.evidence == {
        "event_count": 2,
        "kind": "provider_http",
        "stage": "agent_execution",
        "type": "LLMResponseError",
        "provider": "openai",
        "http_status": 429,
        "retryable": "true",
    }
    assert "must-not-be-exposed" not in str(provider_incident)

    legacy_incident = incidents["run-legacy-llm"]
    assert legacy_incident.message == (
        "LLM request failed, but this trace did not retain the provider error classification."
    )
    assert legacy_incident.evidence == {"event_count": 3, "type": "LLMResponseError"}

    provider_diagnosis = service.get_diagnosis("smith-id", "run-provider-http")
    assert provider_diagnosis.failure_node == "llm:openai"
    assert provider_diagnosis.evidence == [
        "event_count=2",
        "http_status=429",
        "kind=provider_http",
        "provider=openai",
        "retryable=true",
        "stage=agent_execution",
        "type=LLMResponseError",
        "reason=execution_error",
    ]

    legacy_diagnosis = service.get_diagnosis("smith-id", "run-legacy-llm")
    assert legacy_diagnosis.failure_node == "llm"
    assert legacy_diagnosis.recommendation == (
        "Check the LLM provider configuration and availability before retrying; "
        "this trace cannot distinguish the provider failure type."
    )


def test_observability_service_does_not_expose_another_agents_run(tmp_path: Path) -> None:
    service = _service_with_run(tmp_path)

    assert service.list_runs("another-agent", limit=10) == []
    with pytest.raises(HTTPException) as exc:
        service.get_trace("another-agent", "run-1", limit=10)
    assert exc.value.status_code == 404


def test_observability_service_quarantines_a_tampered_trace(tmp_path: Path) -> None:
    service = _service_with_run(tmp_path)
    trace_path = tmp_path / "traces" / "run-1.jsonl"
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    records[0]["data"]["name"] = "forged-shell"
    trace_path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(HTTPException) as exc_info:
        service.get_trace("smith-id", "run-1", limit=10)

    assert exc_info.value.status_code == 409
    diagnosis = service.get_diagnosis("smith-id", "run-1")
    assert diagnosis.primary_category == "trace_integrity"
    proposal = service.get_improvement_proposal("smith-id", "run-1")
    assert proposal.status == "no_action"
    assert proposal.approval_required is False
