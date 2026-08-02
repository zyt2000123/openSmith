from __future__ import annotations

from fastapi import HTTPException

from common import config as common_config
from engine.llm.model_config import SMITH_TEMPLATE_ID

from ..schemas.agent_profile import AgentProfileCreate, AgentProfileUpdate, AgentProfileOut
from ..infrastructure.profile_files import (
    init_smith_profile_files,
)
from ..infrastructure.repositories.agent_profile_repo import AgentProfileRepo

ACTIVE_TEMPLATE_IDS = {SMITH_TEMPLATE_ID}


class AgentProfileService:

    def __init__(self, repo: AgentProfileRepo) -> None:
        self.repo = repo

    async def list_profiles(self) -> list[AgentProfileOut]:
        rows = await self.repo.list_all()
        return [AgentProfileOut(**r) for r in rows]

    async def get_profile(self, agent_id: str) -> AgentProfileOut:
        row = await self.repo.get(agent_id)
        if row is None:
            raise HTTPException(404, "Agent profile not found")
        return AgentProfileOut(**row)

    async def create_profile(self, body: AgentProfileCreate) -> AgentProfileOut:
        if body.role not in ACTIVE_TEMPLATE_IDS:
            allowed = ", ".join(sorted(ACTIVE_TEMPLATE_IDS))
            raise HTTPException(400, f"Unsupported agent role. Allowed: {allowed}")
        data = body.model_dump()
        row = await self.repo.create(data)
        is_new = not row.pop("_existed", False)
        if is_new:
            try:
                init_smith_profile_files(
                    profile_seed_dir=common_config.PATHS.smith_profile_dir,
                    name=body.name,
                    role=body.role,
                    description=body.description,
                )
            except Exception:
                await self.repo.delete(row["id"])
                raise
        return AgentProfileOut(**row)

    async def update_profile(self, agent_id: str, body: AgentProfileUpdate) -> AgentProfileOut:
        updates = body.model_dump(exclude_none=True)
        row = await self.repo.update(agent_id, updates)
        if row is None:
            raise HTTPException(404, "Agent profile not found")
        return AgentProfileOut(**row)

    async def delete_profile(self, agent_id: str) -> None:
        deleted = await self.repo.delete(agent_id)
        if not deleted:
            raise HTTPException(404, "Agent profile not found")
