"""Tests for /api/vault CRUD endpoints and MCP error redaction."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.vault import router as vault_router
from app.services.mcp_service import _safe_error_text
from app.services.vault_service import VaultService
from common.vault import VaultStore


def test_vault_api_crud_never_exposes_plaintext(monkeypatch, tmp_path: Path) -> None:
    vault_path = tmp_path / "vault.json"
    monkeypatch.setattr(VaultService, "_vault_path", vault_path)

    app = FastAPI()
    app.include_router(vault_router)

    with TestClient(app) as client:
        empty = client.get("/api/vault")
        assert empty.status_code == 200
        assert empty.json() == {"entries": []}

        put = client.put(
            "/api/vault/GITHUB_TOKEN",
            json={
                "value": "ghp_super_secret_api_value_12345",
                "description": "GitHub API",
                "allowed_tools": ["mcp_github_*"],
                "allowed_domains": ["api.github.com"],
            },
        )
        assert put.status_code == 200, put.text
        created = put.json()
        assert created["name"] == "GITHUB_TOKEN"
        assert "value" not in created
        assert "ghp_super_secret" not in put.text

        listed = client.get("/api/vault")
        assert listed.status_code == 200
        assert len(listed.json()["entries"]) == 1
        assert "ghp_super_secret" not in listed.text

        # Invalid key name fails closed with 422
        bad = client.put("/api/vault/bad-lower", json={"value": "secret-value-123"})
        assert bad.status_code == 422

        deleted = client.delete("/api/vault/GITHUB_TOKEN")
        assert deleted.status_code == 200
        assert deleted.json()["status"] == "deleted"

        missing = client.delete("/api/vault/GITHUB_TOKEN")
        assert missing.status_code == 404


def test_mcp_safe_error_text_redacts_vault_secrets(tmp_path: Path) -> None:
    store = VaultStore(tmp_path / "vault.json")
    store.set_secret("MCP_TOKEN", "tok_live_secret_value_99999")
    msg = _safe_error_text(
        "HTTP 401 Unauthorized for header Bearer tok_live_secret_value_99999",
        vault_store=store,
    )
    assert "tok_live_secret_value_99999" not in msg
    assert "[REDACTED:vault:MCP_TOKEN]" in msg
