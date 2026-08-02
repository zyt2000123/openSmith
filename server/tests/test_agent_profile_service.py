from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.schemas.agent_profile import AgentProfileCreate  # noqa: E402
from app.services.agent_profile_service import AgentProfileService  # noqa: E402
from common.config import SMITH_PROFILE_DIR  # noqa: E402


class FakeAgentProfileRepo:
    async def list_all(self) -> list[dict]:
        return []

    async def get(self, agent_id: str) -> dict | None:
        return None

    async def create(self, data: dict) -> dict:
        raise AssertionError("create should not be called for unsupported roles")

    async def update(self, agent_id: str, updates: dict) -> dict | None:
        return None

    async def delete(self, agent_id: str) -> bool:
        return False


class FakeCreateAgentProfileRepo(FakeAgentProfileRepo):
    async def create(self, data: dict) -> dict:
        return {
            "id": "smith-id",
            "name": data["name"],
            "role": data["role"],
            "device": data.get("device", ""),
            "online": True,
            "description": data.get("description", ""),
            "knowledge": data.get("knowledge", []),
            "environment": data.get("environment", "本地"),
            "accent": data.get("accent", ""),
            "created_at": "2026-07-09T00:00:00",
        }


@pytest.mark.asyncio
async def test_create_profile_rejects_legacy_template_roles() -> None:
    svc = AgentProfileService(FakeAgentProfileRepo())

    with pytest.raises(HTTPException) as exc:
        await svc.create_profile(AgentProfileCreate(name="Backend", role="backend-engineer"))

    assert exc.value.status_code == 400
    assert "personal-assistant" in exc.value.detail


@pytest.mark.asyncio
async def test_create_profile_seeds_the_single_smith_runtime_profile(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_init_smith_profile_files(**kwargs) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(
        "app.services.agent_profile_service.init_smith_profile_files",
        fake_init_smith_profile_files,
    )

    svc = AgentProfileService(FakeCreateAgentProfileRepo())
    agent = await svc.create_profile(AgentProfileCreate(name="Smith", role="personal-assistant"))

    assert agent.id == "smith-id"
    assert captured["profile_seed_dir"] == SMITH_PROFILE_DIR
