from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from app.services.run_state_service import RunStateService
from engine.execution.orchestration.run_state import RunStateStore
from engine.safety.approval import APPROVAL_BROKER, ApprovalRequest


def test_run_state_service_only_returns_runs_for_current_agent(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path)
    state = store.create(
        "run-1",
        agent_id="smith-id",
        session_id="session-1",
    )
    service = RunStateService(store)

    result = service.get_run("smith-id", state.run_id)

    assert result.run_id == "run-1"
    assert result.status == "queued"
    assert result.session_id == "session-1"

    with pytest.raises(HTTPException) as exc:
        service.get_run("another-agent", state.run_id)
    assert exc.value.status_code == 404


def test_run_state_service_resolves_live_approval_for_current_agent(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path)
    state = store.create("run-1", agent_id="smith-id")
    store.transition("run-1", "running")
    store.request_approval(
        "run-1",
        approval_id="approval-1",
        tool_name="shell",
        level="execute",
        reason="Approval required for shell",
    )
    APPROVAL_BROKER.open(
        ApprovalRequest(
            approval_id="approval-1",
            run_id="run-1",
            tool_name="shell",
            level="execute",
            reason="Approval required for shell",
            arguments_summary={"command": "git status"},
        )
    )

    resolved = RunStateService(store).resolve_approval(
        "smith-id", "run-1", "approval-1", approved=True
    )

    assert resolved.status == "running"
    assert resolved.reason == "approval_granted"
