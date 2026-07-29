from __future__ import annotations

from typing import Any, AsyncGenerator

from common.config import AGENT_DIR
from engine.execution import RunStateStore
from engine.observability import ObservabilityReader
from engine.llm.model_config import SMITH_TEMPLATE_ID

from ..infrastructure.repositories.auto_task_repo import AutoTaskRepo
from ..infrastructure.repositories.agent_profile_repo import AgentProfileRepo
from ..infrastructure.repositories.session_repo import SessionRepo
from ..schemas.agent_profile import AgentProfileOut
from ..schemas.auto_task import AutoTaskCreate, AutoTaskRunOut, AutoTaskUpdate
from ..schemas.memory import MemoryMaintenanceOut
from ..schemas.session import ContextCompressionOut, MessageOut, SessionOut
from ..schemas.run import ApprovalDecision
from ..schemas.mcp import McpServerOut
from .auto_task_service import AutoTaskService
from .agent_profile_service import AgentProfileService
from .project_instruction_service import ProjectInstructionService
from .run_state_service import RunStateService
from .session_service import SessionService
from .skill_service import SkillService
from .mcp_service import McpService
from .observability_service import ObservabilityService
from .token_stats_service import TokenStatsService

SMITH_NAME = "Smith"
SMITH_DESCRIPTION = (
    "面向个人工作流的常驻本地 Agent，负责理解目标、整理上下文、检索信息、"
    "规划执行并交付可落地结果。"
)


class AgentService:
    """Single-Smith facade over the agent-profile persistence model."""

    def __init__(
        self,
        *,
        agent_profile_service: AgentProfileService | Any | None = None,
        session_service: SessionService | Any | None = None,
        auto_task_service: AutoTaskService | Any | None = None,
        skill_service: SkillService | Any | None = None,
        run_state_service: RunStateService | Any | None = None,
        mcp_service: McpService | Any | None = None,
        token_stats_service: TokenStatsService | Any | None = None,
        project_instruction_service: ProjectInstructionService | Any | None = None,
        observability_service: ObservabilityService | Any | None = None,
    ) -> None:
        agent_profile_repo = AgentProfileRepo()
        session_repo = SessionRepo()
        auto_task_repo = AutoTaskRepo()
        run_state_store = (
            RunStateStore(AGENT_DIR)
            if session_service is None or run_state_service is None
            else None
        )
        self.token_stats_service = token_stats_service or TokenStatsService()
        self.agent_profile_service = agent_profile_service or AgentProfileService(agent_profile_repo)
        self.session_service = session_service or SessionService(
            session_repo,
            agent_profile_repo,
            token_stats_service=self.token_stats_service,
            run_state_store=run_state_store,
        )
        self.auto_task_service = auto_task_service or AutoTaskService(
            auto_task_repo,
            agent_profile_repo,
            session_repo,
        )
        self.skill_service = skill_service or SkillService(agent_profile_repo)
        self.run_state_service = run_state_service or RunStateService(run_state_store or RunStateStore(AGENT_DIR))
        self.observability_service = observability_service or ObservabilityService(
            ObservabilityReader(AGENT_DIR)
        )
        self.mcp_service = mcp_service or McpService()
        self.project_instruction_service = project_instruction_service or ProjectInstructionService()

    async def ensure_profile(self) -> AgentProfileOut:
        from ..schemas.agent_profile import AgentProfileCreate

        for profile in await self.agent_profile_service.list_profiles():
            if profile.name == SMITH_NAME and profile.role == SMITH_TEMPLATE_ID:
                return AgentProfileOut(**profile.model_dump())

        created = await self.agent_profile_service.create_profile(
            AgentProfileCreate(
                name=SMITH_NAME,
                role=SMITH_TEMPLATE_ID,
                description=SMITH_DESCRIPTION,
            )
        )
        return AgentProfileOut(**created.model_dump())

    async def get_profile(self) -> AgentProfileOut:
        return await self.ensure_profile()

    async def _profile_id(self) -> str:
        return (await self.ensure_profile()).id

    async def list_sessions(self) -> list[SessionOut]:
        return await self.session_service.list_sessions(await self._profile_id())

    async def create_session(
        self,
        title: str,
        identity_id: str | None = None,
        model_profile: str | None = None,
    ) -> SessionOut:
        return await self.session_service.create_session(
            await self._profile_id(),
            title,
            identity_id,
            model_profile,
        )

    async def update_session_model(self, session_id: str, model_profile: str | None) -> SessionOut:
        return await self.session_service.update_model_profile(
            await self._profile_id(), session_id, model_profile
        )

    async def compress_session(self, session_id: str) -> ContextCompressionOut:
        return await self.session_service.compress_session(await self._profile_id(), session_id)

    async def delete_session(self, session_id: str) -> None:
        await self.session_service.delete_session(await self._profile_id(), session_id)

    def memory_maintenance(self) -> MemoryMaintenanceOut:
        """Report deferred memory maintenance so a client can show it.

        Compilation, periodic candidate curation, and dreaming are scheduled
        as background tasks that outlive the turn scheduling them, so no
        per-run event stream can carry their state. Intentionally synchronous
        and LLM-free: reading a few marker files must not build provider
        clients.
        """
        from engine.memory import memory_maintenance_status

        return MemoryMaintenanceOut(**memory_maintenance_status(AGENT_DIR / "memory"))

    async def list_messages(
        self,
        session_id: str,
        *,
        limit: int = 0,
        offset: int = 0,
    ) -> list[MessageOut]:
        return await self.session_service.list_messages(
            await self._profile_id(),
            session_id,
            limit=limit,
            offset=offset,
        )

    async def stream_message(
        self,
        session_id: str,
        content: str,
        *,
        context: str | None = None,
        skill_name: str | None = None,
        identity_id: str | None = None,
        working_dir: str | None = None,
    ) -> AsyncGenerator[dict, None]:
        stream = await self.prepare_stream_message(
            session_id,
            content,
            context=context,
            skill_name=skill_name,
            identity_id=identity_id,
            working_dir=working_dir,
        )
        async for event in stream:
            yield event

    async def prepare_stream_message(
        self,
        session_id: str,
        content: str,
        *,
        context: str | None = None,
        skill_name: str | None = None,
        identity_id: str | None = None,
        working_dir: str | None = None,
    ) -> AsyncGenerator[dict, None]:
        return await self.session_service.prepare_stream_message(
            await self._profile_id(),
            session_id,
            content,
            context=context,
            skill_name=skill_name,
            identity_id=identity_id,
            working_dir=working_dir,
        )

    async def resume_run(self, run_id: str) -> AsyncGenerator[dict, None]:
        stream = await self.prepare_resume_run(run_id)
        async for event in stream:
            yield event

    async def prepare_resume_run(self, run_id: str) -> AsyncGenerator[dict, None]:
        return await self.session_service.prepare_resume_run(
            await self._profile_id(),
            run_id,
        )

    async def list_skills(self):
        return await self.skill_service.list_skills(await self._profile_id())

    async def set_skill_enabled(self, skill_name: str, enabled: bool):
        return await self.skill_service.set_skill_enabled(await self._profile_id(), skill_name, enabled)

    async def list_mcp_servers(self) -> list[McpServerOut]:
        await self._profile_id()
        return await self.mcp_service.list_servers()

    async def initialize_project_instructions(self, working_dir: str) -> dict:
        result = await self.project_instruction_service.initialize(working_dir)
        return result.model_dump()

    async def get_token_stats(self, year: int | None = None) -> dict:
        await self.token_stats_service.sync_from_traces()
        return await self.token_stats_service.get_stats(await self._profile_id(), year=year)

    async def get_run(self, run_id: str):
        return self.run_state_service.get_run(await self._profile_id(), run_id)

    async def list_observability_runs(self, *, limit: int):
        return self.observability_service.list_runs(await self._profile_id(), limit=limit)

    async def get_observability_run(self, run_id: str):
        return self.observability_service.get_run(await self._profile_id(), run_id)

    async def get_run_trace(self, run_id: str, *, limit: int):
        return self.observability_service.get_trace(await self._profile_id(), run_id, limit=limit)

    async def list_observability_incidents(self, *, limit: int):
        return self.observability_service.list_incidents(await self._profile_id(), limit=limit)

    async def get_run_diagnosis(self, run_id: str):
        return self.observability_service.get_diagnosis(await self._profile_id(), run_id)

    async def get_observability_health(self, *, limit: int):
        return self.observability_service.get_health(await self._profile_id(), limit=limit)

    async def get_run_improvement_proposal(self, run_id: str):
        return self.observability_service.get_improvement_proposal(await self._profile_id(), run_id)

    async def resolve_run_approval(self, run_id: str, decision: ApprovalDecision):
        return self.run_state_service.resolve_approval(
            await self._profile_id(),
            run_id,
            decision.approval_id,
            approved=decision.approved,
        )

    async def list_auto_tasks(self):
        return await self.auto_task_service.list_auto_tasks(await self._profile_id())

    async def create_auto_task(self, body: AutoTaskCreate):
        return await self.auto_task_service.create_auto_task(
            await self._profile_id(),
            body,
        )

    async def update_auto_task(self, task_id: str, body: AutoTaskUpdate):
        return await self.auto_task_service.update_auto_task(task_id, body)

    async def trigger_auto_task(self, task_id: str) -> AutoTaskRunOut:
        return await self.auto_task_service.trigger_auto_task(task_id)

    async def delete_auto_task(self, task_id: str) -> None:
        await self.auto_task_service.delete_auto_task(task_id)

    async def list_runs(self, task_id: str):
        return await self.auto_task_service.list_runs(task_id)
