"""HTTP router for Vault key management (never returns plaintext secret values)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from ..services.vault_service import VaultService

router = APIRouter(prefix="/api/vault", tags=["vault"])


class VaultEntryUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1)
    description: str = ""
    allowed_tools: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)


def get_vault_service() -> VaultService:
    return VaultService()


@router.get("")
async def list_vault_keys(svc: VaultService = Depends(get_vault_service)):
    return {"entries": svc.list_keys()}


@router.put("/{name}")
async def put_vault_key(
    name: str,
    body: VaultEntryUpsert,
    svc: VaultService = Depends(get_vault_service),
):
    return svc.set_key(
        name,
        value=body.value,
        description=body.description,
        allowed_tools=body.allowed_tools,
        allowed_domains=body.allowed_domains,
    )


@router.delete("/{name}")
async def delete_vault_key(
    name: str,
    svc: VaultService = Depends(get_vault_service),
):
    return svc.delete_key(name)
