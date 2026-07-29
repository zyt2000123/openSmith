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


class SessionModelUpdate(BaseModel):
    model_profile: str | None = None


class ContextCompressionOut(BaseModel):
    session_id: str
    summary: str
    message_count: int
    context_summary_cutoff: int


class MessageCreate(BaseModel):
    content: str = Field(max_length=100_000)
    context: str | None = Field(default=None, max_length=50_000)
    skill_name: str | None = None
    identity_id: str | None = None
    working_dir: str = Field(min_length=1)

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
    created_at: str
