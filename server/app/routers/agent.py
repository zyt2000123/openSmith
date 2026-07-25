from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from ..schemas.agent_profile import AgentProfileOut
from ..schemas.auto_task import (
    AutoTaskCreate,
    AutoTaskOut,
    AutoTaskRunOut,
    AutoTaskUpdate,
)
from ..schemas.session import (
    ContextCompressionOut,
    MessageCreate,
    MessageOut,
    SessionCreate,
    SessionModelUpdate,
    SessionOut,
)
from ..schemas.project_instruction import ProjectInstructionInit, ProjectInstructionOut
from ..schemas.run import ApprovalDecision, RunStateOut
from ..schemas.skill import SkillEnabledUpdate, SkillSummaryOut
from ..schemas.mcp import McpServerOut
from ..schemas.observability import AgentHealthOut, RunDiagnosisOut, RunImprovementProposalOut, RunIncidentOut, RunSummaryOut, RunTraceEventOut
from ..schemas.token_stats import TokenStatsOut
from ..services.agent_service import AgentService

router = APIRouter(prefix="/api/agent", tags=["agent"])


def get_agent_service() -> AgentService:
    return AgentService()


@router.get("", response_model=AgentProfileOut)
async def get_profile(svc: AgentService = Depends(get_agent_service)):
    return await svc.get_profile()


@router.post("/ensure", response_model=AgentProfileOut, status_code=201)
async def ensure_profile(svc: AgentService = Depends(get_agent_service)):
    return await svc.ensure_profile()


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(svc: AgentService = Depends(get_agent_service)):
    return await svc.list_sessions()


@router.post("/sessions", response_model=SessionOut, status_code=201)
async def create_session(
    body: SessionCreate,
    svc: AgentService = Depends(get_agent_service),
):
    return await svc.create_session(body.title, body.identity_id, body.model_profile)


@router.patch("/sessions/{session_id}/model", response_model=SessionOut)
async def update_session_model(
    session_id: str,
    body: SessionModelUpdate,
    svc: AgentService = Depends(get_agent_service),
):
    return await svc.update_session_model(session_id, body.model_profile)


@router.post("/sessions/{session_id}/compress", response_model=ContextCompressionOut)
async def compress_session(
    session_id: str,
    svc: AgentService = Depends(get_agent_service),
):
    return await svc.compress_session(session_id)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    svc: AgentService = Depends(get_agent_service),
):
    await svc.delete_session(session_id)


@router.get("/sessions/{session_id}/messages", response_model=list[MessageOut])
async def list_messages(
    session_id: str,
    limit: int = Query(default=0, ge=0),
    offset: int = Query(default=0, ge=0),
    svc: AgentService = Depends(get_agent_service),
):
    return await svc.list_messages(session_id, limit=limit, offset=offset)


@router.post("/sessions/{session_id}/messages/stream")
async def stream_message(
    session_id: str,
    body: MessageCreate,
    svc: AgentService = Depends(get_agent_service),
):
    stream = await svc.prepare_stream_message(
        session_id,
        body.content,
        context=body.context,
        skill_name=body.skill_name,
        identity_id=body.identity_id,
        working_dir=body.working_dir,
    )
    return EventSourceResponse(stream)


@router.get("/skills", response_model=list[SkillSummaryOut])
async def list_skills(svc: AgentService = Depends(get_agent_service)):
    return await svc.list_skills()


@router.put("/skills/{skill_name}", response_model=SkillSummaryOut)
async def set_skill_enabled(
    skill_name: str,
    body: SkillEnabledUpdate,
    svc: AgentService = Depends(get_agent_service),
):
    return await svc.set_skill_enabled(skill_name, body.enabled)


@router.get("/mcp", response_model=list[McpServerOut])
async def list_mcp_servers(svc: AgentService = Depends(get_agent_service)):
    return await svc.list_mcp_servers()


@router.put("/project-instructions", response_model=ProjectInstructionOut)
async def initialize_project_instructions(
    body: ProjectInstructionInit,
    svc: AgentService = Depends(get_agent_service),
):
    return await svc.initialize_project_instructions(body.working_dir)


@router.get("/token-stats", response_model=TokenStatsOut)
async def get_token_stats(
    year: int | None = Query(default=None, ge=2000, le=2100),
    svc: AgentService = Depends(get_agent_service),
):
    return await svc.get_token_stats(year)


@router.get("/observability/runs", response_model=list[RunSummaryOut])
async def list_observability_runs(
    limit: int = Query(default=50, ge=1, le=200),
    svc: AgentService = Depends(get_agent_service),
):
    return await svc.list_observability_runs(limit=limit)


@router.get("/observability/runs/{run_id}", response_model=RunSummaryOut)
async def get_observability_run(
    run_id: str,
    svc: AgentService = Depends(get_agent_service),
):
    return await svc.get_observability_run(run_id)


@router.get("/observability/runs/{run_id}/trace", response_model=list[RunTraceEventOut])
async def get_run_trace(
    run_id: str,
    limit: int = Query(default=300, ge=1, le=1000),
    svc: AgentService = Depends(get_agent_service),
):
    return await svc.get_run_trace(run_id, limit=limit)


@router.get("/observability/incidents", response_model=list[RunIncidentOut])
async def list_observability_incidents(
    limit: int = Query(default=50, ge=1, le=200),
    svc: AgentService = Depends(get_agent_service),
):
    return await svc.list_observability_incidents(limit=limit)


@router.get("/observability/health", response_model=AgentHealthOut)
async def get_observability_health(
    limit: int = Query(default=50, ge=1, le=200),
    svc: AgentService = Depends(get_agent_service),
):
    return await svc.get_observability_health(limit=limit)


@router.get("/observability/runs/{run_id}/diagnosis", response_model=RunDiagnosisOut)
async def get_run_diagnosis(
    run_id: str,
    svc: AgentService = Depends(get_agent_service),
):
    return await svc.get_run_diagnosis(run_id)


@router.get("/observability/runs/{run_id}/improvement-proposal", response_model=RunImprovementProposalOut)
async def get_run_improvement_proposal(
    run_id: str,
    svc: AgentService = Depends(get_agent_service),
):
    return await svc.get_run_improvement_proposal(run_id)


@router.get("/runs/{run_id}", response_model=RunStateOut)
async def get_run(
    run_id: str,
    svc: AgentService = Depends(get_agent_service),
):
    return await svc.get_run(run_id)


@router.post("/runs/{run_id}/resume")
async def resume_run(
    run_id: str,
    svc: AgentService = Depends(get_agent_service),
):
    stream = await svc.prepare_resume_run(run_id)
    return EventSourceResponse(stream)


@router.post("/runs/{run_id}/approval", response_model=RunStateOut)
async def resolve_run_approval(
    run_id: str,
    body: ApprovalDecision,
    svc: AgentService = Depends(get_agent_service),
):
    return await svc.resolve_run_approval(run_id, body)


@router.get("/auto-tasks", response_model=list[AutoTaskOut])
async def list_auto_tasks(svc: AgentService = Depends(get_agent_service)):
    return await svc.list_auto_tasks()


@router.post("/auto-tasks", response_model=AutoTaskOut, status_code=201)
async def create_auto_task(
    body: AutoTaskCreate,
    svc: AgentService = Depends(get_agent_service),
):
    return await svc.create_auto_task(body)


@router.put("/auto-tasks/{task_id}", response_model=AutoTaskOut)
async def update_auto_task(
    task_id: str,
    body: AutoTaskUpdate,
    svc: AgentService = Depends(get_agent_service),
):
    return await svc.update_auto_task(task_id, body)


@router.post("/auto-tasks/{task_id}/trigger", response_model=AutoTaskRunOut)
async def trigger_auto_task(
    task_id: str,
    svc: AgentService = Depends(get_agent_service),
):
    return await svc.trigger_auto_task(task_id)


@router.delete("/auto-tasks/{task_id}", status_code=204)
async def delete_auto_task(
    task_id: str,
    svc: AgentService = Depends(get_agent_service),
):
    await svc.delete_auto_task(task_id)


@router.get("/auto-tasks/{task_id}/runs", response_model=list[AutoTaskRunOut])
async def list_runs(
    task_id: str,
    svc: AgentService = Depends(get_agent_service),
):
    return await svc.list_runs(task_id)
