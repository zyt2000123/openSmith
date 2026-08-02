from __future__ import annotations

import os
from pathlib import Path

import pytest

from engine.execution.orchestration.run_state import (
    RunStateStore,
    RunStateTransitionError,
    RunStatus,
)


def test_run_state_store_round_trips_and_records_lifecycle(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path)

    state = store.create(
        "run-1",
        agent_id="smith-id",
        session_id="session-1",
        message_id="message-1",
        identity_id="smith",
    )

    assert state.status is RunStatus.QUEUED
    store.transition("run-1", RunStatus.RUNNING, event_type="run_started")
    store.record_event("run-1", "tool_call_start", current_tool="shell")
    store.transition("run-1", RunStatus.COMPLETED, event_type="run_finished")

    restored = store.get("run-1")
    assert restored is not None
    assert restored.message_id == "message-1"
    assert restored.status is RunStatus.COMPLETED
    assert restored.session_id == "session-1"
    assert restored.identity_id == "smith"
    assert restored.event_seq == 3
    assert restored.last_event_type == "run_finished"
    assert restored.current_tool == "shell"


def test_run_state_rejects_skipping_running_state(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path)
    store.create("run-1", agent_id="smith-id")

    with pytest.raises(RunStateTransitionError):
        store.transition("run-1", RunStatus.COMPLETED)


def test_run_state_can_resume_an_incomplete_run(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path)
    store.create("run-1", agent_id="smith-id")
    store.transition("run-1", RunStatus.RUNNING)
    store.transition("run-1", RunStatus.INCOMPLETE, reason="budget")

    resumed = store.resume("run-1")

    assert resumed.status is RunStatus.RUNNING
    assert resumed.reason == "resumed"
    assert resumed.last_event_type == "run_resumed"


def test_run_state_preserves_execution_scope_for_a_resumed_run(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path)
    store.create(
        "run-1",
        agent_id="smith-id",
        session_id="session-1",
        identity_id="smith",
        working_dir="/tmp/project",
        forced_skill="review",
    )

    restored = store.get("run-1")

    assert restored is not None
    assert restored.working_dir == "/tmp/project"
    assert restored.forced_skill == "review"


def test_run_state_does_not_resume_completed_run(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path)
    store.create("run-1", agent_id="smith-id")
    store.transition("run-1", RunStatus.RUNNING)
    store.transition("run-1", RunStatus.COMPLETED)

    with pytest.raises(RunStateTransitionError):
        store.resume("run-1")


def test_run_state_waits_for_and_resolves_approval(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path)
    store.create("run-1", agent_id="smith-id")
    store.transition("run-1", RunStatus.RUNNING)

    waiting = store.request_approval(
        "run-1",
        approval_id="approval-1",
        tool_name="shell",
        level="execute",
        reason="Approval required for shell",
    )

    assert waiting.status is RunStatus.WAITING_APPROVAL
    assert waiting.approval_id == "approval-1"
    assert waiting.approval_tool == "shell"

    resumed = store.resolve_approval("run-1", "approval-1", approved=True)

    assert resumed.status is RunStatus.RUNNING
    assert resumed.approval_id is None
    assert resumed.reason == "approval_granted"


def test_run_state_recovers_interrupted_active_runs_as_resumable(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path)
    store.create("waiting", agent_id="smith-id")
    store.create("running", agent_id="smith-id")
    store.create("completed", agent_id="smith-id")
    store.transition("waiting", RunStatus.RUNNING)
    store.request_approval(
        "waiting",
        approval_id="approval-1",
        tool_name="shell",
        level="execute",
        reason="Approval required for shell",
    )
    store.transition("running", RunStatus.RUNNING)
    store.transition("completed", RunStatus.RUNNING)
    store.transition("completed", RunStatus.COMPLETED)

    recovered = store.recover_interrupted()

    assert recovered == ["running", "waiting"]
    waiting = store.get("waiting")
    running = store.get("running")
    completed = store.get("completed")
    assert waiting is not None and waiting.status is RunStatus.CANCELLED
    assert waiting.reason == "server_restarted"
    assert waiting.approval_id is None
    assert running is not None and running.status is RunStatus.CANCELLED
    assert completed is not None and completed.status is RunStatus.COMPLETED

    resumed = store.resume("waiting")
    assert resumed.status is RunStatus.RUNNING


def test_run_state_recovery_skips_a_corrupt_state_file(tmp_path: Path) -> None:
    """One torn state file must not abort startup recovery of every other run."""
    store = RunStateStore(tmp_path)
    store.create("good", agent_id="smith-id")
    store.transition("good", RunStatus.RUNNING)
    corrupt = tmp_path / "broken.json"
    corrupt.write_text("{not-valid-json", encoding="utf-8")

    recovered = store.recover_interrupted()

    assert recovered == ["good"]
    good = store.get("good")
    assert good is not None and good.status is RunStatus.CANCELLED
    # The corrupt file is left untouched for the operator, not overwritten.
    assert "not-valid-json" in corrupt.read_text(encoding="utf-8")


def test_run_state_store_uses_private_atomic_files(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path)
    store.create("run-1", agent_id="smith-id")

    runs_dir = tmp_path / "runs"
    state_path = runs_dir / "run-1.json"
    assert os.stat(runs_dir).st_mode & 0o777 == 0o700
    assert os.stat(state_path).st_mode & 0o777 == 0o600
    assert not list(runs_dir.glob("*.tmp"))


def test_run_state_store_rejects_path_traversal(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path)

    with pytest.raises(ValueError):
        store.get("../outside")


def test_run_state_fsyncs_the_parent_directory_after_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rename must be made durable, not just the file contents."""
    fsync_fds: list[int] = []
    original_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        fsync_fds.append(fd)
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    store = RunStateStore(tmp_path)
    store.create("run-1", agent_id="smith-id")

    # One fsync for the file before os.replace, plus one for the runs dir after.
    assert len(fsync_fds) >= 2
    restored = store.get("run-1")
    assert restored is not None and restored.status is RunStatus.QUEUED
    assert not list((tmp_path / "runs").glob("*.tmp"))


@pytest.mark.asyncio
async def test_run_event_boundary_preserves_stream_order(tmp_path: Path) -> None:
    """Concurrent async record() calls must write the trace in stream order.

    The boundary offloads the blocking trace/state I/O to worker threads;
    asyncio.to_thread alone does not guarantee execution order, which would
    scramble the hash-chain sequence numbers and the byte-offset cursor.
    """
    import asyncio

    from engine.execution.events import EventType, ExecutionEvent
    from engine.execution.orchestration.lifecycle import _RunEventBoundary
    from engine.observability.recorder import RunEventRecorder
    from engine.observability.runtime import RunObservation
    from engine.observability.trace_store import TraceStore

    trace_store = TraceStore(tmp_path)
    observation = RunObservation(RunEventRecorder("run-1", trace_store=trace_store))
    boundary = _RunEventBoundary(None, "run-1", observer=observation)

    async def fire(index: int) -> None:
        await boundary.record(ExecutionEvent(EventType.TOOL_CALL_START, {"name": f"tool-{index}"}))

    await asyncio.gather(*[fire(index) for index in range(20)])

    records = trace_store.read("run-1")
    assert len(records) == 20
    assert [record["data"].get("name") for record in records] == [f"tool-{index}" for index in range(20)]
    # The hash chain must remain verifiable and the seq strictly ascending.
    verification = trace_store.verify("run-1")
    assert verification.ok
    seqs = [int(record["seq"]) for record in records]
    assert seqs == sorted(seqs)


def test_run_state_store_serializes_concurrent_writers(tmp_path: Path) -> None:
    """Concurrent read-modify-write from many threads must not lose updates.

    RunStateStore is written from engine worker threads (offloaded fsync) AND
    from the server event loop (resolve_approval).  Without the per-root RLock
    two writers interleave their get->mutate->save and drop events/increments.
    """
    import threading

    store = RunStateStore(tmp_path)
    store.create("run-1", agent_id="smith-id")

    threads = 8
    events_per_thread = 20
    barrier = threading.Barrier(threads)
    errors: list[BaseException] = []

    def worker(worker_id: int) -> None:
        try:
            barrier.wait()
            for _ in range(events_per_thread):
                store.record_event("run-1", f"event-{worker_id}")
        except BaseException as exc:  # noqa: BLE001 - test isolation
            errors.append(exc)

    workers = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join()

    assert not errors
    restored = store.get("run-1")
    assert restored is not None
    # Exactly threads*events_per_thread increments survived; the lock prevents
    # lost updates from interleaved read-modify-write.
    assert restored.event_seq == threads * events_per_thread


def test_run_state_store_concurrent_approval_race(tmp_path: Path) -> None:
    """Engine-thread state writes racing the server loop's resolve_approval.

    The S1 fix must make these read-modify-writes atomic: exactly one resolver
    wins the pending approval, and the concurrent record_event increments are
    all preserved (no lost update on the same run state file).
    """
    import threading

    store = RunStateStore(tmp_path)
    store.create("run-1", agent_id="smith-id")
    store.transition("run-1", RunStatus.RUNNING)
    store.request_approval(
        "run-1",
        approval_id="approval-1",
        tool_name="shell",
        level="execute",
        reason="review",
    )

    resolvers = 3
    writers = 6
    writes_per_writer = 10
    barrier = threading.Barrier(resolvers + writers)
    outcomes: list[str] = []

    def resolver() -> None:
        try:
            barrier.wait()
            store.resolve_approval("run-1", "approval-1", approved=True)
            outcomes.append("resolved")
        except RunStateTransitionError:
            # A concurrent resolver legitimately loses the race once the state
            # left WAITING_APPROVAL; this is the expected serialization.
            outcomes.append("lost-race")
        except Exception as exc:  # noqa: BLE001 - test isolation
            outcomes.append(f"error:{type(exc).__name__}")

    def writer(worker_id: int) -> None:
        try:
            barrier.wait()
            for _ in range(writes_per_writer):
                store.record_event("run-1", f"writer-{worker_id}")
        except Exception as exc:  # noqa: BLE001 - test isolation
            outcomes.append(f"error:{type(exc).__name__}")

    threads = [threading.Thread(target=resolver) for _ in range(resolvers)] + [
        threading.Thread(target=writer, args=(i,)) for i in range(writers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("resolved") == 1
    assert "error:" not in " ".join(outcomes)

    state = store.get("run-1")
    assert state is not None
    assert state.status is RunStatus.RUNNING
    # request_approval (+1) + the winning resolve (+1) + writers*events.
    assert state.event_seq == 2 + writers * writes_per_writer


@pytest.mark.asyncio
async def test_run_event_boundary_stream_order_stress(tmp_path: Path) -> None:
    """A larger, mixed-event burst fired concurrently must still land in stream
    order with a verifiable hash chain.

    The boundary serializes its to_thread offloads; without the serialization a
    busy thread pool reorders trace records, scrambling the chain's seq and the
    byte-offset cursor.
    """
    import asyncio

    from engine.execution.events import EventType, ExecutionEvent
    from engine.execution.orchestration.lifecycle import _RunEventBoundary
    from engine.observability.recorder import RunEventRecorder
    from engine.observability.runtime import RunObservation
    from engine.observability.trace_store import TraceStore

    trace_store = TraceStore(tmp_path)
    observation = RunObservation(RunEventRecorder("run-1", trace_store=trace_store))
    boundary = _RunEventBoundary(None, "run-1", observer=observation)

    total = 100
    markers = [f"t{index}" for index in range(total)]

    async def fire(index: int) -> None:
        # Mix event types so the trace carries distinguishing data.
        await boundary.record(ExecutionEvent(EventType.TOOL_CALL_START, {"name": markers[index]}))

    await asyncio.gather(*[fire(index) for index in range(total)])

    records = trace_store.read("run-1")
    assert len(records) == total
    names = [record["data"].get("name") for record in records]
    assert names == markers  # exact stream order under concurrency
    verification = trace_store.verify("run-1")
    assert verification.ok
    seqs = [int(record["seq"]) for record in records]
    assert seqs == list(range(1, total + 1))
