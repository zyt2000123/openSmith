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
