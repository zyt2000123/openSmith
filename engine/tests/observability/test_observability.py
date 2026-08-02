from __future__ import annotations

from pathlib import Path

from engine.execution.events import EventType, ExecutionEvent
from engine.observability import (
    HealthCalculator,
    IncidentDetector,
    ObservabilityRetentionPolicy,
    RunDiagnoser,
    RunEventRecorder,
    RunMetadata,
    RunObservation,
    RunSummary,
    RunSummaryRecord,
    RunSummaryStore,
    TraceStore,
)


def test_recorder_persists_events_and_exposes_a_compact_run_summary(tmp_path) -> None:
    projected: list[str] = []
    recorder = RunEventRecorder(
        "run-1",
        trace_store=TraceStore(tmp_path),
        projections=(lambda event: projected.append(event.type.value),),
    )

    recorder.record(ExecutionEvent(EventType.RUN_STARTED, {"run_id": "run-1"}))
    recorder.record(ExecutionEvent(EventType.TOOL_CALL_START, {"name": "shell"}))
    recorder.record(ExecutionEvent(EventType.BACKTRACK, {"from": "plan", "to": "research"}))
    recorder.record(ExecutionEvent(EventType.TOKEN_USAGE, {
        "input_tokens": 100,
        "output_tokens": 25,
        "total_tokens": 125,
    }))
    recorder.record(ExecutionEvent(EventType.RUN_FINISHED, {
        "run_id": "run-1",
        "status": "incomplete",
        "reason": "tool_call_budget",
    }))

    summary = recorder.summary()
    assert [record["type"] for record in TraceStore(tmp_path).read("run-1")] == [
        "run_started",
        "tool_call_start",
        "backtrack",
        "token_usage",
        "run_finished",
    ]
    assert projected == [
        "run_started",
        "tool_call_start",
        "backtrack",
        "token_usage",
        "run_finished",
    ]
    assert summary.event_count == 5
    assert summary.tool_call_count == 1
    assert summary.backtrack_count == 1
    assert summary.token_usage == {
        "input_tokens": 100,
        "output_tokens": 25,
        "total_tokens": 125,
    }
    assert summary.outcome == "incomplete"
    assert summary.reason == "tool_call_budget"


def test_recorder_continues_projecting_when_trace_write_fails() -> None:
    class FailingTraceStore:
        def append(self, run_id: str, event: ExecutionEvent) -> None:
            raise OSError("disk unavailable")

    projected: list[str] = []
    recorder = RunEventRecorder(
        "run-1",
        trace_store=FailingTraceStore(),  # type: ignore[arg-type]
        projections=(lambda event: projected.append(event.type.value),),
    )

    recorder.record(ExecutionEvent(EventType.FAILED, {"reason": "provider_error"}))

    assert projected == ["failed"]
    assert recorder.summary().event_counts == {"failed": 1}


def test_observation_persists_the_route_selected_during_execution(tmp_path) -> None:
    from engine.execution.events import RunObservationContext

    observation = RunObservation.start(RunObservationContext(
        run_id="run-route",
        agent_id="smith",
        profile_dir=tmp_path,
        created_at="2026-08-02T00:00:00+00:00",
    ))
    observation.record(ExecutionEvent(EventType.ROUTE_DECIDED, {
        "identity_id": "coding",
        "route_id": "tdd-development",
        "pipeline_id": "tdd-development",
    }))
    observation.record(ExecutionEvent(EventType.RUN_FINISHED, {"status": "completed"}))

    record = RunSummaryStore(tmp_path).get("run-route")

    assert record is not None
    assert record.metadata.identity_id == "coding"
    assert record.metadata.route_id == "tdd-development"
    assert record.metadata.pipeline_id == "tdd-development"


def test_summary_store_persists_aggregate_only_and_merges_resumed_run(tmp_path) -> None:
    store = RunSummaryStore(tmp_path)
    metadata = RunMetadata(
        run_id="run-1",
        agent_id="smith",
        session_id="session-1",
        working_dir="/project",
        created_at="2026-07-19T00:00:00+00:00",
    )
    first = RunEventRecorder("run-1", summary_sinks=(lambda summary: store.save(metadata, summary),))
    first.record(ExecutionEvent(EventType.TOOL_CALL_START, {"name": "shell"}))
    first.record(ExecutionEvent(EventType.RUN_FINISHED, {
        "status": "cancelled",
        "reason": "consumer_disconnected",
    }))

    resumed = RunEventRecorder("run-1", summary_sinks=(lambda summary: store.save(metadata, summary),))
    resumed.record(ExecutionEvent(EventType.TOOL_CALL_START, {"name": "search"}))
    resumed.record(ExecutionEvent(EventType.RUN_FINISHED, {"status": "completed"}))

    record = store.get("run-1")
    assert record is not None
    assert record.metadata.agent_id == "smith"
    assert record.metadata.session_id == "session-1"
    assert record.summary.tool_call_count == 2
    assert record.summary.outcome == "completed"
    assert record.summary.reason is None
    assert "raw prompt" not in (tmp_path / "runs" / "run-1.summary.json").read_text(encoding="utf-8")


def _save_completed_summary(
    store: RunSummaryStore,
    run_id: str,
    *,
    agent_id: str = "smith",
) -> None:
    metadata = RunMetadata(
        run_id=run_id,
        agent_id=agent_id,
        created_at="2026-07-29T00:00:00+00:00",
    )
    recorder = RunEventRecorder(
        run_id,
        summary_sinks=(lambda summary: store.save(metadata, summary),),
    )
    recorder.record(ExecutionEvent(EventType.RUN_FINISHED, {"status": "completed"}))


def test_summary_list_reads_only_the_index_selected_records(
    tmp_path,
    monkeypatch,
) -> None:
    store = RunSummaryStore(tmp_path)
    for run_id in ("run-1", "run-2", "run-3"):
        _save_completed_summary(store, run_id)

    summary_reads: list[str] = []
    original_read_text = Path.read_text

    def recording_read_text(path: Path, *args, **kwargs):
        if path.name.endswith(".summary.json"):
            summary_reads.append(path.name)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", recording_read_text)

    records = store.list("smith", limit=1)

    assert [record.metadata.run_id for record in records] == ["run-3"]
    assert summary_reads == ["run-3.summary.json"]


def test_summary_index_reconciles_after_a_failed_upsert(tmp_path, monkeypatch) -> None:
    """A run whose index upsert failed (e.g. SQLITE_BUSY) must not stay hidden
    from list() forever: the next access re-bootstraps the idempotent index."""
    store = RunSummaryStore(tmp_path)
    _save_completed_summary(store, "run-1")

    # Simulate the concurrent-writer failure on the next upsert.
    import sqlite3

    def failing_bootstrap_entry(entry):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store._index, "bootstrap_entry", failing_bootstrap_entry)
    _save_completed_summary(store, "run-2")
    assert store._index_stale is True

    # The next access re-bootstraps the idempotent index from the summary
    # files on disk, so run-2 is no longer hidden.
    records = store.list("smith", limit=10)
    assert store._index_stale is False
    assert {record.metadata.run_id for record in records} == {"run-1", "run-2"}


def test_observability_retention_removes_oldest_completed_run_files(
    tmp_path,
) -> None:
    policy = ObservabilityRetentionPolicy(
        max_completed_runs=2,
        max_age_days=None,
        max_bytes=None,
    )
    store = RunSummaryStore(tmp_path, retention=policy)
    traces = TraceStore(tmp_path)
    for run_id in ("run-1", "run-2", "run-3"):
        traces.append(run_id, ExecutionEvent(EventType.RUN_FINISHED, {"status": "completed"}))
        _save_completed_summary(store, run_id)

    assert store.get("run-1") is None
    assert not (tmp_path / "traces" / "run-1.jsonl").exists()
    assert [
        record.metadata.run_id
        for record in store.list("smith", limit=10)
    ] == ["run-3", "run-2"]


def test_observability_retention_keeps_the_newest_oversized_run(
    tmp_path,
) -> None:
    policy = ObservabilityRetentionPolicy(
        max_completed_runs=None,
        max_age_days=None,
        max_bytes=1,
    )
    store = RunSummaryStore(tmp_path, retention=policy)

    _save_completed_summary(store, "run-latest")

    assert store.get("run-latest") is not None


def test_observability_retention_environment_only_uses_zero_to_disable(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AGENT_SMITH_OBSERVABILITY_MAX_RUNS", "-1")
    monkeypatch.setenv("AGENT_SMITH_OBSERVABILITY_MAX_AGE_DAYS", "0")

    policy = ObservabilityRetentionPolicy.from_environment()

    assert policy.max_completed_runs == 2_000
    assert policy.max_age_days is None


def _completed_record(outcome: str) -> RunSummaryRecord:
    return RunSummaryRecord(
        schema_version=1,
        metadata=RunMetadata(
            run_id="run-x",
            agent_id="smith",
            created_at="2026-08-02T00:00:00+00:00",
        ),
        finished_at="2026-08-02T00:01:00+00:00",
        summary=RunSummary(
            run_id="run-x",
            event_count=1,
            event_counts={},
            tool_call_count=1,
            backtrack_count=0,
            approval_required_count=0,
            token_usage={},
            outcome=outcome,
            reason=None,
        ),
    )


def test_incident_detector_flags_genuine_tool_timeouts() -> None:
    record = _completed_record("completed")
    trace = [
        {"type": "tool_call_result", "data": {
            "error": True,
            "blocked": False,
            "preflight": False,
            "content": "Tool timed out after 30s",
            "error_kind": "timeout",
            "retryable": True,
            "timed_out": True,
            "side_effect_status": "unknown",
        }},
        {"type": "run_finished", "data": {"status": "completed"}},
    ]

    incidents = IncidentDetector().detect(record, trace)

    timeouts = [incident for incident in incidents if incident.category == "tool_timeout"]
    assert len(timeouts) == 1
    assert timeouts[0].evidence["timeout_count"] == 1


def test_incident_detector_does_not_flag_approval_timeout_as_tool_timeout() -> None:
    record = _completed_record("completed")
    trace = [
        {"type": "tool_call_result", "data": {
            "blocked": True,
            "error": False,
            "preflight": False,
            "reason": "Approval timed out",
        }},
        {"type": "run_finished", "data": {"status": "completed"}},
    ]

    incidents = IncidentDetector().detect(record, trace)

    assert all(incident.category != "tool_timeout" for incident in incidents)
    assert incidents == []


def test_diagnosis_recovers_timeout_failure_node_and_evidence() -> None:
    record = _completed_record("completed")
    trace = [
        {"type": "tool_call_start", "data": {"name": "shell"}},
        {"type": "tool_call_result", "data": {
            "name": "shell",
            "error": True,
            "blocked": False,
            "error_kind": "timeout",
            "timed_out": True,
        }},
        {"type": "run_finished", "data": {"status": "completed"}},
    ]

    diagnosis = RunDiagnoser().diagnose(record, trace)

    assert diagnosis.status == "needs_attention"
    assert diagnosis.primary_category == "tool_timeout"
    assert diagnosis.failure_node == "tool:shell"
    assert "tool=shell" in diagnosis.evidence


def test_incident_detector_never_emits_run_blocked_from_run_finished() -> None:
    # RUN_FINISHED producers emit only completed/incomplete/failed/cancelled;
    # "blocked" exists only on SKILL_END and is not a run terminal status.
    record = _completed_record("blocked")
    trace = [{"type": "run_finished", "data": {"status": "blocked"}}]

    incidents = IncidentDetector().detect(record, trace)

    assert all(incident.category != "run_blocked" for incident in incidents)
    assert incidents == []


def test_health_tool_success_rate_ignores_approval_required_events() -> None:
    record = RunSummaryRecord(
        schema_version=1,
        metadata=RunMetadata(
            run_id="run-1",
            agent_id="smith",
            created_at="2026-08-02T00:00:00+00:00",
        ),
        finished_at="2026-08-02T00:01:00+00:00",
        summary=RunSummary(
            run_id="run-1",
            event_count=3,
            event_counts={"tool_call_result": 2, "run_finished": 1},
            tool_call_count=1,
            backtrack_count=0,
            approval_required_count=1,
            token_usage={},
            outcome="completed",
            reason=None,
        ),
    )
    traces = [
        [
            {"type": "tool_call_result", "data": {
                "blocked": True,
                "approval_required": True,
                "error": False,
            }},
            {"type": "tool_call_result", "data": {
                "error": False,
                "content": "ok",
                "approval_outcome": "granted",
            }},
            {"type": "run_finished", "data": {"status": "completed"}},
        ]
    ]

    health = HealthCalculator().calculate("smith", [record], traces)

    # The gate probe must not count as a phantom failure: one approved,
    # successful tool yields 100%, not 50%.
    assert health.tool_success_rate == 1.0
    assert health.run_count == 1
    assert health.completed_count == 1


def test_health_tool_success_rate_ignores_denied_approvals() -> None:
    """A user-denied tool call never executed, so it must not count as a tool
    failure — only the one real successful tool is measured."""
    record = RunSummaryRecord(
        schema_version=1,
        metadata=RunMetadata(
            run_id="run-1",
            agent_id="smith",
            created_at="2026-08-02T00:00:00+00:00",
        ),
        finished_at="2026-08-02T00:01:00+00:00",
        summary=RunSummary(
            run_id="run-1",
            event_count=3,
            event_counts={"tool_call_result": 2, "run_finished": 1},
            tool_call_count=1,
            backtrack_count=0,
            approval_required_count=1,
            token_usage={},
            outcome="completed",
            reason=None,
        ),
    )
    traces = [
        [
            {"type": "tool_call_result", "data": {
                "blocked": True,
                "approval_required": True,
                "error": False,
            }},
            {"type": "tool_call_result", "data": {
                "blocked": True,
                "approval_outcome": "denied",
                "error": False,
            }},
            {"type": "tool_call_result", "data": {
                "error": False,
                "content": "ok",
                "approval_outcome": "granted",
            }},
            {"type": "run_finished", "data": {"status": "completed"}},
        ]
    ]

    health = HealthCalculator().calculate("smith", [record], traces)

    # The gate probe and the denied approval are non-executions; only the
    # granted, successful tool counts — 100%, not 33%.
    assert health.tool_success_rate == 1.0
