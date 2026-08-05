from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.routers.agent import get_agent_service, router


def test_delete_session_route_delegates_to_agent_service() -> None:
    calls: list[str] = []

    class FakeAgentService:
        async def delete_session(self, session_id: str) -> None:
            calls.append(session_id)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_service] = lambda: FakeAgentService()

    with TestClient(app) as client:
        response = client.delete("/api/agent/sessions/session-1")

    assert response.status_code == 204
    assert response.content == b""
    assert calls == ["session-1"]


def test_token_stats_route_returns_local_usage_dashboard_data() -> None:
    class FakeAgentService:
        async def get_token_stats(self, year: int | None = None) -> dict:
            return {
                "year": year or 2026,
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "session_count": 1,
                "active_days": 1,
                "current_streak": 1,
                "longest_streak": 1,
                "favorite_model": "gpt-test",
                "peak_hour": 10,
                "daily": [],
                "models": [],
            }

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_service] = lambda: FakeAgentService()

    with TestClient(app) as client:
        response = client.get("/api/agent/token-stats?year=2026")

    assert response.status_code == 200
    assert response.json()["total_tokens"] == 15
    assert response.json()["year"] == 2026


def test_observability_routes_delegate_to_agent_service() -> None:
    class FakeAgentService:
        async def list_observability_runs(self, *, limit: int) -> list[dict]:
            assert limit == 20
            return [{
                "run_id": "run-1", "agent_id": "smith", "created_at": "2026-07-19T00:00:00+00:00",
                "finished_at": "2026-07-19T00:01:00+00:00", "event_count": 2, "event_counts": {},
                "tool_call_count": 0, "backtrack_count": 0, "approval_required_count": 0,
                "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            }]

        async def get_observability_run(self, run_id: str) -> dict:
            assert run_id == "run-1"
            return (await self.list_observability_runs(limit=20))[0]

        async def get_run_trace(self, run_id: str, *, limit: int) -> list[dict]:
            assert run_id == "run-1"
            assert limit == 10
            return [{"seq": 1, "timestamp": "2026-07-19T00:00:00+00:00", "run_id": run_id, "type": "done", "data": {}}]

        async def list_observability_incidents(self, *, limit: int) -> list[dict]:
            assert limit == 20
            return [{
                "run_id": "run-1", "agent_id": "smith", "severity": "error",
                "category": "tool_timeout", "message": "One or more tool calls timed out.",
                "occurred_at": "2026-07-19T00:01:00+00:00", "evidence": {"timeout_count": 1},
            }]

        async def get_observability_health(self, *, limit: int) -> dict:
            assert limit == 20
            return {
                "agent_id": "smith", "run_count": 1, "completed_count": 1,
                "unsuccessful_count": 0, "success_rate": 1.0, "tool_call_count": 1,
                "tool_success_rate": 1.0, "average_backtracks": 0.0,
                "total_tokens": 15, "tokens_per_run": 15.0,
            }

        async def get_run_diagnosis(self, run_id: str) -> dict:
            assert run_id == "run-1"
            return {
                "run_id": run_id, "agent_id": "smith", "status": "needs_attention",
                "failure_node": "tool:shell", "primary_category": "tool_timeout",
                "summary": "One or more tool calls timed out.", "evidence": ["tool=shell"],
                "recommendation": "Review the affected tool's timeout and retry policy; validate the target before retrying.",
            }

        async def get_run_improvement_proposal(self, run_id: str) -> dict:
            assert run_id == "run-1"
            return {
                "run_id": run_id, "agent_id": "smith", "status": "proposed",
                "category": "tool_timeout", "title": "Review tool timeout policy",
                "rationale": "One or more tool calls timed out.",
                "suggested_change": "Propose a scoped timeout or retry-policy adjustment for the affected tool after validating its target.",
                "approval_required": True,
            }

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_service] = lambda: FakeAgentService()

    with TestClient(app) as client:
        runs = client.get("/api/agent/observability/runs?limit=20")
        detail = client.get("/api/agent/observability/runs/run-1")
        trace = client.get("/api/agent/observability/runs/run-1/trace?limit=10")
        incidents = client.get("/api/agent/observability/incidents?limit=20")
        health = client.get("/api/agent/observability/health?limit=20")
        diagnosis = client.get("/api/agent/observability/runs/run-1/diagnosis")
        proposal = client.get("/api/agent/observability/runs/run-1/improvement-proposal")

    assert runs.status_code == 200
    assert detail.json()["run_id"] == "run-1"
    assert trace.json()[0]["seq"] == 1
    assert incidents.json()[0]["category"] == "tool_timeout"
    assert health.json()["success_rate"] == 1.0
    assert diagnosis.json()["failure_node"] == "tool:shell"
    assert proposal.json()["approval_required"] is True


def test_project_instruction_route_delegates_to_agent_service() -> None:
    calls: list[str] = []

    class FakeAgentService:
        async def initialize_project_instructions(self, working_dir: str) -> dict:
            calls.append(working_dir)
            return {"path": "/workspace/project/.smith/SMITH.md", "created": True}

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_service] = lambda: FakeAgentService()

    with TestClient(app) as client:
        response = client.put("/api/agent/project-instructions", json={"working_dir": "/workspace/project"})

    assert response.status_code == 200
    assert response.json() == {"path": "/workspace/project/.smith/SMITH.md", "created": True}
    assert calls == ["/workspace/project"]


def test_resume_run_route_delegates_to_the_streaming_agent_service() -> None:
    calls: list[str] = []

    class FakeAgentService:
        async def prepare_resume_run(self, run_id: str):
            calls.append(run_id)

            async def stream():
                yield {"event": "done", "data": '{"run_id":"run-1","status":"completed"}'}

            return stream()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_service] = lambda: FakeAgentService()

    with TestClient(app) as client:
        response = client.post("/api/agent/runs/run-1/resume")

    assert response.status_code == 200
    assert "event: done" in response.text
    assert calls == ["run-1"]


def test_resume_run_route_returns_preflight_errors_before_opening_sse() -> None:
    class FakeAgentService:
        async def prepare_resume_run(self, _run_id: str):
            raise HTTPException(409, "Run cannot be resumed from completed")

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_service] = lambda: FakeAgentService()

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/agent/runs/run-1/resume")

    assert response.status_code == 409
    assert response.json()["detail"] == "Run cannot be resumed from completed"


def test_stream_route_returns_preflight_errors_before_opening_sse() -> None:
    class FakeAgentService:
        async def prepare_stream_message(self, *_args, **_kwargs):
            raise HTTPException(404, "Session not found")

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_service] = lambda: FakeAgentService()

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/agent/sessions/missing/messages/stream",
            json={"content": "hello", "working_dir": "/tmp/project"},
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Session not found"


def test_memory_status_route_reports_deferred_maintenance() -> None:
    class FakeAgentService:
        def memory_maintenance(self) -> dict:
            return {
                "compile": "idle",
                "nudge": "running",
                "dream": "running",
                "consecutive_failures": 213,
                "last_error": "LLMResponseError: LLM request failed (HTTP 401)",
            }

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_service] = lambda: FakeAgentService()

    with TestClient(app) as client:
        response = client.get("/api/agent/memory/status")

    assert response.status_code == 200
    assert response.json() == {
        "compile": "idle",
        "nudge": "running",
        "dream": "running",
        "consecutive_failures": 213,
        "last_error": "LLMResponseError: LLM request failed (HTTP 401)",
    }


def test_memory_status_rejects_an_unknown_state() -> None:
    """The schema is the contract: an unexpected state must not reach clients."""

    class FakeAgentService:
        def memory_maintenance(self) -> dict:
            return {"compile": "idle", "nudge": "idle", "dream": "sleepwalking"}

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_service] = lambda: FakeAgentService()

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/agent/memory/status")

    assert response.status_code == 500
