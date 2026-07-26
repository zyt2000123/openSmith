import math
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..services.config_service import ConfigService

router = APIRouter(prefix="/api/config", tags=["config"])

LLMUsageName = Literal["interactive", "gate", "background"]


class LLMRoutePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    stream: bool | None = None
    max_output_tokens: int | None = Field(default=None, gt=0)
    context_window: int | None = Field(default=None, gt=0)
    timeout_profile: LLMUsageName | None = None

    @field_validator("max_output_tokens", mode="before")
    @classmethod
    def validate_max_output_tokens(cls, value: object) -> object:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("max_output_tokens must be an integer")
        return value


class LLMTimeoutProfilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connect: float | None = Field(default=None, gt=0)
    read: float | None = Field(default=None, gt=0)
    stream_read: float | None = Field(default=None, gt=0)
    write: float | None = Field(default=None, gt=0)
    pool: float | None = Field(default=None, gt=0)

    @field_validator("connect", "read", "stream_read", "write", "pool", mode="before")
    @classmethod
    def validate_timeout_number(cls, value: object) -> object:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            raise ValueError("timeout values must be finite numbers")
        return value


class LLMConfig(BaseModel):
    """Patch request for the persisted LLM configuration.

    Omitted fields are preserved.  Sending ``null`` removes an override;
    sending an empty routes/profile mapping clears that entire section.
    """

    model_config = ConfigDict(extra="forbid")

    vendor: str | None = None
    provider: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    stream: bool | None = None
    max_output_tokens: int | None = Field(default=None, gt=0)
    context_window: int | None = Field(default=None, gt=0)
    routes: dict[LLMUsageName, LLMRoutePatch | None] | None = None
    models: dict[str, LLMRoutePatch | None] | None = None
    timeout_profiles: dict[LLMUsageName, LLMTimeoutProfilePatch | None] | None = None

    @field_validator("max_output_tokens", mode="before")
    @classmethod
    def validate_max_output_tokens(cls, value: object) -> object:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("max_output_tokens must be an integer")
        return value


def get_config_service() -> ConfigService:
    return ConfigService()


@router.get("/llm")
async def get_llm_config(svc: ConfigService = Depends(get_config_service)):
    return svc.get_llm_config()


@router.get("/llm/models")
async def list_relay_models(svc: ConfigService = Depends(get_config_service)):
    return await svc.list_relay_models()


@router.post("/llm")
async def set_llm_config(body: LLMConfig, svc: ConfigService = Depends(get_config_service)):
    return svc.set_llm_config(updates=body.model_dump(exclude_unset=True))
