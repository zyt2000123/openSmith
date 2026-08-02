from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from common import config as common_config
from common.paths import AppPaths
from engine.skill.loader import SkillBody
from engine.skill.registry import SkillRegistry
from engine.skill.settings import SkillSettingsError, disabled_skill_names, set_skill_enabled as persist_skill_enabled

from ..schemas.skill import SkillSummaryOut
from ..infrastructure.repositories.agent_profile_repo import AgentProfileRepo


# Compatibility seams for isolated service tests. Production paths are read
# when the method runs, so a config.reset_paths() is observed consistently.
AGENT_DIR: Path | None = None
BUILTIN_SKILLS_DIR: Path | None = None
PATHS: AppPaths | None = None


def _paths() -> AppPaths:
    return PATHS if PATHS is not None else common_config.PATHS


def _agent_dir() -> Path:
    return AGENT_DIR if AGENT_DIR is not None else _paths().agent_dir


def _builtin_skills_dir() -> Path:
    return BUILTIN_SKILLS_DIR if BUILTIN_SKILLS_DIR is not None else _paths().builtin_skills_dir


class SkillService:

    def __init__(self, agent_profile_repo: AgentProfileRepo) -> None:
        self.agent_profile_repo = agent_profile_repo

    async def list_skills(self, agent_id: str) -> list[SkillSummaryOut]:
        await self._ensure_profile(agent_id)
        registry = self._load_registry()
        disabled = self._disabled_skills()

        summaries: list[SkillSummaryOut] = []
        for item in sorted(registry.list_summaries(), key=lambda value: value["name"]):
            body = registry.get(item["name"])
            if body is None:
                continue
            summaries.append(self._summary(body, item["source"], disabled))
        return summaries

    async def ensure_skill_exists(self, agent_id: str, skill_name: str) -> SkillSummaryOut:
        await self._ensure_profile(agent_id)
        registry = self._load_registry()
        body = registry.get(skill_name)
        if body is None:
            raise HTTPException(404, f"Skill '{skill_name}' not found")

        source = "builtin" if registry.is_builtin(skill_name) else "agent"
        return self._summary(body, source, self._disabled_skills())

    async def set_skill_enabled(self, agent_id: str, skill_name: str, enabled: bool) -> SkillSummaryOut:
        await self._ensure_profile(agent_id)
        registry = self._load_registry()
        body = registry.get(skill_name)
        if body is None:
            raise HTTPException(404, f"Skill '{skill_name}' not found")

        disabled = self._persist_skill_enabled(skill_name, enabled)
        source = "builtin" if registry.is_builtin(skill_name) else "agent"
        return self._summary(body, source, disabled)

    @staticmethod
    def _summary(body: SkillBody, source: str, disabled: set[str]) -> SkillSummaryOut:
        return SkillSummaryOut(
            name=body.meta.name,
            description=body.meta.description,
            source=source,
            version=body.meta.version,
            argument_hint=body.meta.argument_hint,
            enabled=body.meta.name not in disabled,
        )

    async def _ensure_profile(self, agent_id: str) -> None:
        if await self.agent_profile_repo.get(agent_id) is None:
            raise HTTPException(404, "Agent profile not found")

    @staticmethod
    def _disabled_skills() -> set[str]:
        try:
            return disabled_skill_names(_agent_dir())
        except SkillSettingsError as exc:
            raise HTTPException(422, f"Invalid skill settings: {exc}") from exc

    @staticmethod
    def _persist_skill_enabled(skill_name: str, enabled: bool) -> set[str]:
        try:
            return persist_skill_enabled(_agent_dir(), skill_name, enabled=enabled)
        except SkillSettingsError as exc:
            raise HTTPException(422, f"Invalid skill settings: {exc}") from exc

    @staticmethod
    def _load_registry() -> SkillRegistry:
        registry = SkillRegistry()
        paths = _paths()
        paths.ensure_base_dirs()
        registry.load_builtin(_builtin_skills_dir())

        agent_skills_dir = _agent_dir() / "skills"
        if agent_skills_dir.is_dir():
            registry.load_agent_skills(agent_skills_dir)
        return registry
