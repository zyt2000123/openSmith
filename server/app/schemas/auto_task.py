from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

TriggerType = Literal["manual", "cron", "interval"]


class AutoTaskCreate(BaseModel):
    title: str
    description: str = ""
    trigger_type: TriggerType = "manual"
    trigger_config: str = ""
    instruction: str
    working_dir: str = Field(min_length=1)
    enabled: bool = True
    max_retries: int = 2

    @model_validator(mode="after")
    def _validate_trigger_config(self) -> "AutoTaskCreate":
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if self.trigger_type == "cron" and not self.trigger_config.strip():
            raise ValueError("trigger_config is required for cron trigger_type")
        if self.trigger_type == "interval":
            cfg = self.trigger_config.strip()
            if not cfg:
                raise ValueError("trigger_config is required for interval trigger_type")
            try:
                val = int(cfg)
            except ValueError:
                raise ValueError("trigger_config must be an integer (seconds) for interval trigger_type")
            if val <= 0:
                raise ValueError("interval trigger_config must be a positive integer")
        return self

    @field_validator("working_dir")
    @classmethod
    def _validate_working_dir(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("working_dir must not be blank")
        return value.strip()


class AutoTaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    trigger_type: TriggerType | None = None
    trigger_config: str | None = None
    instruction: str | None = None
    working_dir: str | None = None
    enabled: bool | None = None
    max_retries: int | None = None

    @field_validator("max_retries")
    @classmethod
    def _validate_max_retries(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("max_retries must be non-negative")
        return value

    @field_validator("working_dir")
    @classmethod
    def _validate_working_dir(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("working_dir must not be blank")
        return value.strip() if value is not None else None


class AutoTaskOut(BaseModel):
    id: str
    agent_id: str
    title: str
    description: str
    trigger_type: str
    trigger_config: str
    instruction: str
    working_dir: str
    enabled: bool
    status: str
    last_run_at: str | None = None
    next_run_at: str | None = None
    run_count: int
    retry_count: int = 0
    max_retries: int = 2
    lease_until: str | None = None
    created_at: str


class AutoTaskRunOut(BaseModel):
    id: str
    auto_task_id: str
    status: str
    output: str
    started_at: str
    finished_at: str | None = None
    error: str | None = None
