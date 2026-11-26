from typing import Literal

from pydantic import BaseModel, Field, field_validator


class SessionCreate(BaseModel):
    title: str = "新对话"
    identity_id: str | None = None
    model_profile: str | None = None


class SessionOut(BaseModel):
    id: str
    agent_id: str
    identity_id: str | None = None
    model_profile: str | None = None
    title: str
    created_at: str
    last_message_preview: str | None = None
    last_message_at: str | None = None
    message_count: int = 0
    session_kind: str = "main"
    parent_session_id: str | None = None
    snapshot_cutoff_message_id: str | None = None


class SideSessionCreate(BaseModel):
    title: str = "Side chat"
    reuse_existing: bool = True


class PromoteSideRequest(BaseModel):
    title: str | None = None
    keep_parent_context: bool = True


class SessionModelUpdate(BaseModel):
    model_profile: str | None = None


class ContextCompressionOut(BaseModel):
    session_id: str
    summary: str
    message_count: int
    context_summary_cutoff: int


class BudgetOverride(BaseModel):
    """Optional per-request budget tightening (cannot raise platform limits)."""

    max_total_tokens: int | None = Field(default=None, gt=0)
    max_wall_seconds: float | None = Field(default=None, gt=0)
    max_cost_usd: float | None = Field(default=None, gt=0)
    unpriced_cost_policy: Literal["warn_only", "fail_closed"] | None = None


class MessageCreate(BaseModel):
    content: str = Field(max_length=100_000)
    context: str | None = Field(default=None, max_length=50_000)
    skill_name: str | None = None
    identity_id: str | None = None
    working_dir: str = Field(min_length=1)
    budget: BudgetOverride | None = None

    @field_validator("working_dir")
    @classmethod
    def _validate_working_dir(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("working_dir must not be blank")
        return value.strip()


class MessageOut(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    kind: str = "normal"
    parent_message_id: str | None = None
    created_at: str


class SteerCreate(BaseModel):
    content: str = Field(min_length=1, max_length=10_000)
    run_id: str | None = None

    @field_validator("content")
    @classmethod
    def _validate_content(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("content must not be blank")
        return trimmed


class SteerOut(BaseModel):
    accepted: bool
    status: str
    run_id: str | None = None
    steer_id: str | None = None
    pending_count: int = 0
