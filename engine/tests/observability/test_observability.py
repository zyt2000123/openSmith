from __future__ import annotations

from pathlib import Path

from engine.execution.events import EventType, ExecutionEvent
from engine.observability import (
    ObservabilityRetentionPolicy,
    RunEventRecorder,
    RunMetadata,
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
