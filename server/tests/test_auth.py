from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import main  # noqa: E402
from app.infrastructure import auth  # noqa: E402


def test_auth_token_write_refuses_a_preplanted_symlink(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """The token file must be created atomically, never written through a symlink
    that a local attacker could plant (e.g. a dangling link pointing at an
    arbitrary file) to clobber that file when the server starts."""
    token_path = tmp_path / "auth_token"
    outside = tmp_path / "victim.txt"  # does not exist yet; a dangling link
    token_path.symlink_to(outside)  # would have the server create it on start

    monkeypatch.setattr(auth, "_TOKEN_PATH", token_path)
    monkeypatch.setattr(auth, "_cached_token", None)
    monkeypatch.setattr(auth, "_cached_token_path", None)

    with pytest.raises(RuntimeError, match="symlink"):
        auth.get_local_token()

    assert not outside.exists()
    assert token_path.is_symlink()


def test_server_lifespan_materializes_local_auth_token_before_shell_requests(
    monkeypatch,
    tmp_path: Path,
) -> None:
    token_path = tmp_path / "auth_token"
    monkeypatch.setattr(auth, "_TOKEN_PATH", token_path)
    monkeypatch.setattr(auth, "_cached_token", None)

    async def fake_get_app_db():
        return None

    async def fake_close_db() -> None:
        return None

    async def fake_close_clients() -> None:
        return None

    async def fake_scheduler() -> None:
        await asyncio.sleep(3600)

    class FakeTokenStatsService:
        async def sync_from_traces(self) -> int:
            return 0

        async def record_generation(self, record) -> None:
            return None

    monkeypatch.setattr(main, "get_app_db", fake_get_app_db)
    monkeypatch.setattr(main, "close_db", fake_close_db)
    monkeypatch.setattr(main, "close_shared_llm_clients", fake_close_clients)
    monkeypatch.setattr(main, "run_scheduler", fake_scheduler)
    monkeypatch.setattr(main, "load_runtime_identity_catalog", lambda force=False: None)
    monkeypatch.setattr(main, "TokenStatsService", FakeTokenStatsService)

    with TestClient(main.app):
        assert token_path.is_file()
        assert token_path.read_text(encoding="utf-8").strip()


def test_server_lifespan_syncs_token_stats_before_serving_requests(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class FakeTokenStatsService:
        async def sync_from_traces(self) -> int:
            calls.append("sync")
            return 0

        async def record_generation(self, record) -> None:
            return None

    async def fake_get_app_db():
        return None

    async def fake_close_db() -> None:
        return None

    async def fake_close_clients() -> None:
        return None

    async def fake_scheduler() -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(main, "get_local_token", lambda: "test-token")
    monkeypatch.setattr(main, "get_app_db", fake_get_app_db)
    monkeypatch.setattr(main, "close_db", fake_close_db)
    monkeypatch.setattr(main, "close_shared_llm_clients", fake_close_clients)
    monkeypatch.setattr(main, "run_scheduler", fake_scheduler)
    monkeypatch.setattr(main, "load_runtime_identity_catalog", lambda force=False: None)
    monkeypatch.setattr(main, "TokenStatsService", FakeTokenStatsService)

    with TestClient(main.app):
        assert calls == ["sync"]


def test_server_lifespan_recovers_interrupted_runs_before_serving_requests(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class FakeRunStateStore:
        def __init__(self, _profile_dir: Path) -> None:
            calls.append("store")

        def recover_interrupted(self) -> list[str]:
            calls.append("recover")
            return ["run-1"]

    class FakeTokenStatsService:
        async def sync_from_traces(self) -> int:
            return 0

        async def record_generation(self, record) -> None:
            return None

    async def fake_get_app_db():
        return None

    async def fake_close_db() -> None:
        return None

    async def fake_close_clients() -> None:
        return None

    async def fake_scheduler() -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(main, "get_local_token", lambda: "test-token")
    monkeypatch.setattr(main, "get_app_db", fake_get_app_db)
    monkeypatch.setattr(main, "close_db", fake_close_db)
    monkeypatch.setattr(main, "close_shared_llm_clients", fake_close_clients)
    monkeypatch.setattr(main, "run_scheduler", fake_scheduler)
    monkeypatch.setattr(main, "load_runtime_identity_catalog", lambda force=False: None)
    monkeypatch.setattr(main, "TokenStatsService", FakeTokenStatsService)
    monkeypatch.setattr(main, "RunStateStore", FakeRunStateStore)

    with TestClient(main.app):
        assert calls == ["store", "recover"]


def test_server_lifespan_survives_unavailable_run_state_storage(
    monkeypatch,
) -> None:
    class UnavailableRunStateStore:
        def __init__(self, _profile_dir: Path) -> None:
            raise OSError("permission denied")

    class FakeTokenStatsService:
        async def sync_from_traces(self) -> int:
            return 0

        async def record_generation(self, record) -> None:
            return None

    async def fake_get_app_db():
        return None

    async def fake_close_db() -> None:
        return None

    async def fake_close_clients() -> None:
        return None

    async def fake_scheduler() -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(main, "get_local_token", lambda: "test-token")
    monkeypatch.setattr(main, "get_app_db", fake_get_app_db)
    monkeypatch.setattr(main, "close_db", fake_close_db)
    monkeypatch.setattr(main, "close_shared_llm_clients", fake_close_clients)
    monkeypatch.setattr(main, "run_scheduler", fake_scheduler)
    monkeypatch.setattr(main, "load_runtime_identity_catalog", lambda force=False: None)
    monkeypatch.setattr(main, "TokenStatsService", FakeTokenStatsService)
    monkeypatch.setattr(main, "RunStateStore", UnavailableRunStateStore)

    with TestClient(main.app):
        pass
