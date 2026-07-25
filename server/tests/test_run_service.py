from __future__ import annotations

import inspect
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


def test_approval_resolution_stays_synchronous_end_to_end() -> None:
    """审批批复先落库、后唤醒 broker，这个顺序只在整条链全同步时才安全。

    单线程事件循环下 is_pending 检查到 resolve() 之间没有其他协程能插进来，所以
    当前顺序没有竞态。一旦其中任何一环变成 async（例如把 RunStateStore 换成
    aiosqlite），落库成功而唤醒失败就会让等待审批的引擎协程永远挂住。把"全同步"
    钉成不变量，改造存储的人会先撞到这条测试而不是线上挂死。
    """
    for func in (
        RunStateService.resolve_approval,
        RunStateStore.resolve_approval,
        APPROVAL_BROKER.is_pending,
        APPROVAL_BROKER.resolve,
    ):
        assert not inspect.iscoroutinefunction(func), (
            f"{func.__qualname__} 变成 async 会让审批唤醒出现真实竞态；"
            "改造前请重新审视 RunStateService.resolve_approval 的落库/唤醒顺序"
        )
