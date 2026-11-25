"""Tests for GET/PATCH /api/config/sandbox endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.config import router as config_router
from app.services.config_service import ConfigService


def test_sandbox_config_api_get_patch_and_validation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(ConfigService, "_config_path", config_path)
    app = FastAPI()
    app.include_router(config_router)

    with TestClient(app) as client:
        default_resp = client.get("/api/config/sandbox")
        assert default_resp.status_code == 200
        body = default_resp.json()
        assert body["backend"] == "auto"
        assert body["fallback_policy"] == "deny"
        assert body["default_network"] == "none"
        assert body["auto_detect_caches"] is False

        patch_resp = client.patch(
            "/api/config/sandbox",
            json={
                "extra_roots": ["/tmp/shared-a"],
                "share_read_only": ["/opt/homebrew"],
                "cache_roots": {"uv": "~/.cache/uv"},
                "default_network": "loopback_only",
            },
        )
        assert patch_resp.status_code == 200, patch_resp.text
        updated = patch_resp.json()
        assert updated["status"] == "ok"
        assert updated["extra_roots"] == ["/tmp/shared-a"]
        assert updated["cache_roots"]["uv"] == "~/.cache/uv"

        # Unknown key rejected (extra="forbid")
        bad_key = client.patch("/api/config/sandbox", json={"unknown_field": 1})
        assert bad_key.status_code == 422

        # Control character rejected fail-closed
        bad_path = client.patch(
            "/api/config/sandbox",
            json={"extra_roots": ["/tmp/bad\npath"]},
        )
        assert bad_path.status_code == 422
