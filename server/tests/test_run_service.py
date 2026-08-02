from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.services.run_state_service import RunStateService
from engine.execution import RunStateStore
from engine.safety.approval import APPROVAL_BROKER, ApprovalRequest


@pytest.mark.asyncio
async def test_run_state_service_only_returns_runs_for_current_agent(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path)
    state = store.create(
        "run-1",
        agent_id="smith-id",
        session_id="session-1",
    )
    service = RunStateService(store)

    result = await service.get_run("smith-id", state.run_id)

    assert result.run_id == "run-1"
    assert result.status == "queued"
    assert result.session_id == "session-1"

    with pytest.raises(HTTPException) as exc:
        await service.get_run("another-agent", state.run_id)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_run_state_service_resolves_live_approval_for_current_agent(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path)
    store.create("run-1", agent_id="smith-id")
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

    resolved = await RunStateService(store).resolve_approval(
        "smith-id", "run-1", "approval-1", approved=True
    )

    assert resolved.status == "running"
    assert resolved.reason == "approval_granted"


def test_approval_broker_resolve_stays_synchronous() -> None:
    """审批链中 broker 的检查和唤醒必须是同步的。

    服务端先唤醒 broker（让等待中的引擎协程继续）再异步持久化 run state。
    broker 的 is_pending→resolve 两段之间不能有 await 点，否则引擎超时线程能在
    检查与唤醒之间弹出条目，造成"已唤醒但 store 已落库"的错位。这条测试把
    broker 两段同步钉成不变量。
    """
    for func in (
        APPROVAL_BROKER.is_pending,
        APPROVAL_BROKER.resolve,
    ):
        assert not inspect.iscoroutinefunction(func), (
            f"{func.__qualname__} 变成 async 会让 is_pending→resolve 之间出现"
            "await 点，审批唤醒会出现真实竞态；改造前请重新审视顺序"
        )
