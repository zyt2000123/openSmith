"""Server integration tests for Module 2: Mid-turn Steering API & persistence."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.infrastructure.repositories.agent_profile_repo import AgentProfileRepo
from app.infrastructure.repositories.session_repo import SessionRepo
from app.main import app
from app.services.session_service import SessionService
from common import config as common_config
from common.database import close_db
from engine.execution import EventType, ExecutionEvent, RunStateStore, RunStatus, STEERING_BROKER
from engine.safety.approval import APPROVAL_BROKER, ApprovalRequest


@pytest_asyncio.fixture(autouse=True)
async def _close_db_after_test():
    yield
    await close_db()


def _auth_headers() -> dict[str, str]:
    from app.infrastructure.auth import get_local_token

    return {"Authorization": f"Bearer {get_local_token()}"}


@pytest.mark.asyncio
async def test_steer_api_no_active_run_returns_409():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=_auth_headers()) as client:
        sess_resp = await client.post("/api/agent/sessions", json={"title": "steer-test"})
        assert sess_resp.status_code == 201
        sid = sess_resp.json()["id"]

        steer_resp = await client.post(
            f"/api/agent/sessions/{sid}/steer",
            json={"content": "change course"},
        )
        assert steer_resp.status_code == 409
        body = steer_resp.json()
        assert body["accepted"] is False
        assert body["status"] == "no_active_run"

        # No DB row should have been written
        msgs = await client.get(f"/api/agent/sessions/{sid}/messages")
        assert msgs.status_code == 200
        assert msgs.json() == []


@pytest.mark.asyncio
async def test_steer_api_while_waiting_approval_queues_without_resolving():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=_auth_headers()) as client:
        sess_resp = await client.post("/api/agent/sessions", json={"title": "approval-steer"})
        assert sess_resp.status_code == 201
        sid = sess_resp.json()["id"]
        agent_id = sess_resp.json()["agent_id"]

        run_id = "run-waiting-approval"
        store = RunStateStore(common_config.PATHS.agent_dir)
        store.create(run_id, agent_id=agent_id, session_id=sid)
        store.transition(run_id, RunStatus.RUNNING)
        store.request_approval(
            run_id,
            approval_id="appr-1",
            tool_name="shell",
            level="execute",
            reason="Approval required",
        )
        APPROVAL_BROKER.open(
            ApprovalRequest(
                approval_id="appr-1",
                run_id=run_id,
                tool_name="shell",
                level="execute",
                reason="Approval required",
                arguments_summary={"command": "ls"},
            )
        )
        inbox = STEERING_BROKER.open_run(run_id, sid)
        try:
            resp = await client.post(
                f"/api/agent/sessions/{sid}/steer",
                json={"content": "actually skip shell", "run_id": run_id},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["accepted"] is True
            assert data["status"] == "queued_while_waiting_approval"
            assert data["run_id"] == run_id
            assert data["pending_count"] == 1
            # Approval MUST still be pending (never bypassed!)
            assert APPROVAL_BROKER.is_pending(run_id, "appr-1") is True
            assert inbox.pending_count() == 1
        finally:
            STEERING_BROKER.close_run(run_id)
            APPROVAL_BROKER.cancel_run(run_id)


@pytest.mark.asyncio
async def test_stream_persists_steering_applied_and_resume_accepts_steering_rows(
    monkeypatch,
    tmp_path: Path,
):
    """STEERING_APPLIED persists kind='steering', and resuming that run does NOT 409 and carries steering_history."""
    profile_repo = AgentProfileRepo()
    session_repo = SessionRepo()
    store = RunStateStore(common_config.PATHS.agent_dir)
    service = SessionService(session_repo, profile_repo, run_state_store=store)

    # Ensure Smith profile + session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=_auth_headers()) as client:
        sess_resp = await client.post("/api/agent/sessions", json={"title": "steer-resume"})
        assert sess_resp.status_code == 201
        sid = sess_resp.json()["id"]
        agent_id = sess_resp.json()["agent_id"]

    captured_requests = []

    class _FakeStream:
        def __init__(self, run_id: str, events: list[ExecutionEvent]) -> None:
            self.run_id = run_id
            self._events = events

        async def stream_events(self):
            for ev in self._events:
                yield ev

        async def aclose(self):
            return None

    async def fake_build_runtime(self_svc, _agent_id, _profile_name, _session_id):
        return SimpleNamespace(), SimpleNamespace(llm=SimpleNamespace(model="test-model"))

    def fake_run_stream(request, runtime, services):
        captured_requests.append(request)
        run_id = "run-steer-1"
        store.create(
            run_id,
            agent_id=agent_id,
            session_id=sid,
            message_id=request.message_id,
            identity_id="smith",
            working_dir=request.working_dir,
        )
        store.transition(run_id, RunStatus.RUNNING)
        store.transition(run_id, RunStatus.INCOMPLETE, reason="interrupted")
        return _FakeStream(
            run_id,
            [
                ExecutionEvent(EventType.RUN_STARTED, {"run_id": run_id}),
                ExecutionEvent(
                    EventType.STEERING_APPLIED,
                    {
                        "steer_ids": ["st_1"],
                        "count": 1,
                        "items": [{"steer_id": "st_1", "content": "only touch src/"}],
                        "iteration": 1,
                    },
                ),
                ExecutionEvent(EventType.TEXT_DELTA, {"text": "partial reply"}),
                ExecutionEvent(EventType.INCOMPLETE, {"reason": "interrupted"}),
                ExecutionEvent(EventType.RUN_FINISHED, {"run_id": run_id, "status": "incomplete", "reason": "interrupted"}),
            ],
        )

    def fake_resume_stream(request, runtime, services, resume_run_id):
        captured_requests.append(request)
        return _FakeStream(
            resume_run_id,
            [
                ExecutionEvent(EventType.RUN_STARTED, {"run_id": resume_run_id}),
                ExecutionEvent(EventType.TEXT_DELTA, {"text": "resumed final reply"}),
                ExecutionEvent(EventType.RUN_FINISHED, {"run_id": resume_run_id, "status": "completed"}),
            ],
        )

    monkeypatch.setattr(SessionService, "_build_runtime", fake_build_runtime)
    monkeypatch.setattr("app.services.session_service.engine_run_stream_with_runtime", fake_run_stream)
    monkeypatch.setattr("app.services.session_service.engine_resume_stream_with_runtime", fake_resume_stream)

    # 1. Run initial stream with steering_applied event
    stream = await service.prepare_stream_message(
        agent_id,
        sid,
        "fix the bug",
        working_dir=str(tmp_path),
    )
    sse_events = [ev async for ev in stream]
    steer_sse = [ev for ev in sse_events if ev["event"] == "steering_applied"]
    assert len(steer_sse) == 1
    assert json.loads(steer_sse[0]["data"])["steer_ids"] == ["st_1"]

    # Verify kind='steering' persisted in DB
    all_msgs = await session_repo.get_messages(sid)
    steering_msgs = [m for m in all_msgs if m.get("kind") == "steering"]
    assert len(steering_msgs) == 1
    assert steering_msgs[0]["content"] == "only touch src/"
    assert steering_msgs[0]["parent_message_id"] == captured_requests[0].message_id

    # 2. Resume the interrupted run — MUST NOT 409 despite kind='steering' user row!
    resume_gen = await service.prepare_resume_run(agent_id, "run-steer-1")
    resume_events = [ev async for ev in resume_gen]
    assert any(ev["event"] == "done" for ev in resume_events)
    assert len(captured_requests) == 2
    assert captured_requests[1].steering_history == ("only touch src/",)

    # Stale partial assistant reply was replaced cleanly
    final_msgs = await session_repo.get_messages(sid)
    assistant_msgs = [m for m in final_msgs if m["role"] == "assistant"]
    assert [m["content"] for m in assistant_msgs] == ["resumed final reply"]
