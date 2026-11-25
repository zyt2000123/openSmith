"""Tests for GET /api/agent/doctor and POST /api/agent/doctor/probe endpoints."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.agent import get_agent_service, router as agent_router


def test_agent_doctor_endpoints_forward_to_service() -> None:
    calls: list[tuple[str | None, bool]] = []

    class FakeAgentService:
        async def run_doctor(self, *, cwd: str | None = None, run_probes: bool = True) -> dict:
            calls.append((cwd, run_probes))
            return {
                "ok": True,
                "platform": "darwin",
                "cwd": cwd or "/tmp",
                "sandbox_backend": "seatbelt",
                "fallback_policy": "deny",
                "checks": [],
                "duration_ms": 1,
            }

    app = FastAPI()
    app.include_router(agent_router)
    app.dependency_overrides[get_agent_service] = FakeAgentService

    with TestClient(app) as client:
        r_static = client.get("/api/agent/doctor?cwd=/tmp/ws")
        assert r_static.status_code == 200
        assert r_static.json()["ok"] is True

        r_probe = client.post("/api/agent/doctor/probe?cwd=/tmp/ws")
        assert r_probe.status_code == 200
        assert r_probe.json()["sandbox_backend"] == "seatbelt"

    assert calls == [("/tmp/ws", False), ("/tmp/ws", True)]


def test_agent_service_run_doctor_enforces_timeout_504(monkeypatch) -> None:
    """M7: AgentService.run_doctor wraps run_doctor_async in asyncio.wait_for -> 504 on timeout."""
    import asyncio
    import pytest
    from fastapi import HTTPException

    import engine.sandbox.doctor as doctor_mod
    from app.services.agent_service import AgentService

    async def slow_doctor(**_kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(doctor_mod, "run_doctor_async", slow_doctor)

    real_wait_for = asyncio.wait_for

    async def fast_timeout(coro, timeout=None):
        return await real_wait_for(coro, timeout=0.01)

    monkeypatch.setattr(asyncio, "wait_for", fast_timeout)

    svc = object.__new__(AgentService)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(svc.run_doctor(cwd="/tmp", run_probes=True))
    assert exc_info.value.status_code == 504
