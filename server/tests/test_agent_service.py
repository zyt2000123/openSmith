from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.agent_service import AgentService  # noqa: E402


class FakeProfile(SimpleNamespace):
    def model_dump(self):
        return dict(self.__dict__)


class FakeAgentProfileService:
    def __init__(self, rows):
        self.rows = rows
        self.created = 0

    async def list_profiles(self):
        return list(self.rows)

    async def create_profile(self, body):
        self.created += 1
        row = FakeProfile(
            id="smith-id",
            name=body.name,
            role=body.role,
            device="",
            online=True,
            description=body.description,
            knowledge=[],
            environment="本地",
            accent="",
            created_at="2026-07-09T00:00:00",
        )
        self.rows.append(row)
        return row


def profile_row(name="Smith", role="personal-assistant"):
    return FakeProfile(
        id="existing-id",
        name=name,
        role=role,
        device="",
        online=True,
        description="",
        knowledge=[],
        environment="本地",
        accent="",
        created_at="2026-07-09T00:00:00",
    )


@pytest.mark.asyncio
async def test_agent_service_reuses_existing_smith_profile() -> None:
    fake = FakeAgentProfileService([profile_row()])

    profile = await AgentService(agent_profile_service=fake).ensure_profile()

    assert profile.id == "existing-id"
    assert fake.created == 0


@pytest.mark.asyncio
async def test_agent_service_creates_single_smith_profile_when_missing() -> None:
    fake = FakeAgentProfileService([])

    profile = await AgentService(agent_profile_service=fake).ensure_profile()

    assert profile.name == "Smith"
    assert profile.role == "personal-assistant"
    assert fake.created == 1


@pytest.mark.asyncio
async def test_get_token_stats_reads_persisted_stats_without_resyncing_traces() -> None:
    class FakeTokenStatsService:
        sync_calls = 0

        async def sync_from_traces(self) -> int:
            self.sync_calls += 1
            return 0

        async def get_stats(self, agent_id: str, year: int | None = None) -> dict:
            return {"agent_id": agent_id, "year": year}

    token_stats = FakeTokenStatsService()
    stats = await AgentService(
        agent_profile_service=FakeAgentProfileService([profile_row()]),
        token_stats_service=token_stats,
    ).get_token_stats(year=2026)

    assert stats == {"agent_id": "existing-id", "year": 2026}
    assert token_stats.sync_calls == 0
