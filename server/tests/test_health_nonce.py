"""/api/health echoes the launcher's nonce so the shell can identify its server.

Two shells starting on the same empty port both spawn uvicorn; one loses the
bind and dies.  The launcher passes SMITH_SERVER_NONCE and checks the health
response so it never adopts the foreign survivor as "its own" server.
"""

from __future__ import annotations

import asyncio

from app.main import health


def test_health_echoes_the_launch_nonce(monkeypatch) -> None:
    monkeypatch.setenv("SMITH_SERVER_NONCE", "launch-abc-123")

    body = asyncio.run(health())

    assert body["status"] == "ok"
    assert body["nonce"] == "launch-abc-123"


def test_health_nonce_is_null_without_a_launch_nonce(monkeypatch) -> None:
    monkeypatch.delenv("SMITH_SERVER_NONCE", raising=False)

    assert asyncio.run(health())["nonce"] is None


def test_health_nonce_is_null_for_an_empty_env_value(monkeypatch) -> None:
    # An empty string must not read as an identity a launcher could match on.
    monkeypatch.setenv("SMITH_SERVER_NONCE", "")

    assert asyncio.run(health())["nonce"] is None
