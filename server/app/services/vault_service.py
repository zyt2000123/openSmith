"""Service layer for managing local Vault metadata and secrets."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from common import config as common_config
from common.vault import VaultError, VaultStore


class VaultService:
    """Thin service wrapper around :class:`common.vault.VaultStore`.

    Never exposes plaintext secret values through any read or write response.
    """

    _vault_path: Path | None = None

    @classmethod
    def _store(cls) -> VaultStore:
        path = cls._vault_path or common_config.PATHS.vault_path
        return VaultStore(path)

    def list_keys(self) -> list[dict[str, Any]]:
        try:
            return [item.to_dict() for item in self._store().list_metadata()]
        except (VaultError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def set_key(
        self,
        name: str,
        *,
        value: str,
        description: str = "",
        allowed_tools: Iterable[str] = (),
        allowed_domains: Iterable[str] = (),
    ) -> dict[str, Any]:
        try:
            meta = self._store().set_secret(
                name,
                value,
                description=description,
                allowed_tools=allowed_tools,
                allowed_domains=allowed_domains,
            )
            return meta.to_dict()
        except (VaultError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def delete_key(self, name: str) -> dict[str, Any]:
        try:
            deleted = self._store().delete_secret(name)
        except (VaultError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Vault key not found: {name}")
        return {"status": "deleted", "name": name}
