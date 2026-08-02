from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RunSummaryOut(BaseModel):
    run_id: str
    agent_id: str
    session_id: str | None = None
    identity_id: str | None = None
    route_id: str | None = None
    pipeline_id: str | None = None
    working_dir: str | None = None
    forced_skill: str | None = None
    created_at: str
    finished_at: str
    outcome: str | None = None
    reason: str | None = None
    event_count: int = Field(ge=0)
    event_counts: dict[str, int]
    tool_call_count: int = Field(ge=0)
    backtrack_count: int = Field(ge=0)
    approval_required_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class RunTraceEventOut(BaseModel):
    seq: int = Field(ge=1)
    timestamp: str
    run_id: str
    type: str
    data: dict[str, Any]


class RunIncidentOut(BaseModel):
    run_id: str
    agent_id: str
    severity: str
    category: str
    message: str
    reason: str | None = None
    occurred_at: str
    evidence: dict[str, int | str]


class RunDiagnosisOut(BaseModel):
    run_id: str
    agent_id: str
    status: str
    failure_node: str | None = None
    primary_category: str | None = None
    summary: str
    evidence: list[str]
    recommendation: str | None = None


class AgentHealthOut(BaseModel):
    agent_id: str
    run_count: int = Field(ge=0)
    completed_count: int = Field(ge=0)
    unsuccessful_count: int = Field(ge=0)
    success_rate: float = Field(ge=0, le=1)
    tool_call_count: int = Field(ge=0)
    tool_success_rate: float | None = Field(default=None, ge=0, le=1)
    average_backtracks: float = Field(ge=0)
    total_tokens: int = Field(ge=0)
    tokens_per_run: float = Field(ge=0)


class RunImprovementProposalOut(BaseModel):
    run_id: str
    agent_id: str
    status: str
    category: str | None = None
    title: str
    rationale: str
    suggested_change: str | None = None
    approval_required: bool
