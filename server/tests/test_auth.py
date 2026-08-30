from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.paths import AppPaths  # noqa: E402
from engine.execution import EventType, ExecutionEvent, RunObservationContext, RunStateStore  # noqa: E402
from engine.observability import RunObservation, RunSummaryStore, TraceStore  # noqa: E402
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


def test_startup_reconciliation_does_not_resurrect_pruned_runs(
    tmp_path: Path,
) -> None:
    """被保留策略修剪掉的 run 不得在重启对账里复活成僵尸摘要。

    这是 #10 真正的危害，而不是"删了哪几个文件"。旧行为下修剪只删 summary
    和 trace、留下 state，于是"有终态 state、无 summary"对对账不可区分于崩溃
    窗口：每个被修剪的 run 被重新 finalize 成 finished_at=now 的空事件摘要，
    按 finished_at DESC 排到所有真实 run 前面，在同一轮循环里把它们挤出保留
    窗；被挤掉的下次启动又复活，索引最终只剩僵尸。
    """
    paths = AppPaths(data_dir=tmp_path / "data", project_root=tmp_path / "project")
    main.common_config.reset_paths(paths)
    previous_limit = os.environ.get("AGENT_SMITH_OBSERVABILITY_MAX_RUNS")
    os.environ["AGENT_SMITH_OBSERVABILITY_MAX_RUNS"] = "2"
    try:
        store = RunStateStore(paths.agent_dir)
        for run_id in ("run-1", "run-2", "run-3"):
            store.create(run_id, agent_id="smith", session_id="session-1")
            store.transition(run_id, "running")
            observation = RunObservation.start(RunObservationContext(
                run_id=run_id,
                agent_id="smith",
                session_id="session-1",
                profile_dir=paths.agent_dir,
            ))
            observation.record(ExecutionEvent(EventType.RUN_STARTED, {"run_id": run_id}))
            observation.record(ExecutionEvent(EventType.RUN_FINISHED, {
                "run_id": run_id, "status": "completed",
            }))
            # 终态状态文件正是"被修剪 vs 崩溃窗口"歧义的载体：对账只看终态 run。
            store.transition(run_id, "completed")

        survivors = {
            record.metadata.run_id: record.summary.event_count
            for record in RunSummaryStore(paths.agent_dir).list("smith", limit=10)
        }
        assert set(survivors) == {"run-2", "run-3"}, survivors

        main._reconcile_startup_observability(store, recovered_run_ids=[])

        after = {
            record.metadata.run_id: record.summary.event_count
            for record in RunSummaryStore(paths.agent_dir).list("smith", limit=10)
        }
        assert set(after) == set(survivors), f"resurrected: {set(after) - set(survivors)}"
        # 幸存者的事件数没有被重新 finalize 出来的空摘要覆盖。
        assert after == survivors
    finally:
        if previous_limit is None:
            os.environ.pop("AGENT_SMITH_OBSERVABILITY_MAX_RUNS", None)
        else:
            os.environ["AGENT_SMITH_OBSERVABILITY_MAX_RUNS"] = previous_limit
        main.common_config.reset_paths()


def test_startup_reconciliation_closes_a_recovered_run_in_trace_and_summary(
    tmp_path: Path,
) -> None:
    paths = AppPaths(data_dir=tmp_path / "data", project_root=tmp_path / "project")
    main.common_config.reset_paths(paths)
    try:
        store = RunStateStore(paths.agent_dir)
        store.create("run-recovered", agent_id="smith", session_id="session-1")
        store.transition("run-recovered", "running")
        observation = RunObservation.start(RunObservationContext(
            run_id="run-recovered",
            agent_id="smith",
            session_id="session-1",
            profile_dir=paths.agent_dir,
        ))
        observation.record(ExecutionEvent(EventType.RUN_STARTED, {"run_id": "run-recovered"}))

        recovered = store.recover_interrupted()
        main._reconcile_startup_observability(store, recovered_run_ids=recovered)

        trace = TraceStore(paths.agent_dir)
        records = trace.read("run-recovered")
        assert [record["type"] for record in records] == ["run_started", "run_finished"]
        assert records[-1]["data"] == {
            "run_id": "run-recovered",
            "status": "cancelled",
            "reason": "server_restarted",
        }
        assert trace.verify("run-recovered").ok
        summary = RunSummaryStore(paths.agent_dir).get("run-recovered")
        assert summary is not None
        assert summary.summary.outcome == "cancelled"
        assert summary.summary.reason == "server_restarted"
    finally:
        main.common_config.reset_paths()


def test_startup_reconciliation_materializes_a_summary_after_trace_terminal_write(
    tmp_path: Path,
) -> None:
    paths = AppPaths(data_dir=tmp_path / "data", project_root=tmp_path / "project")
    main.common_config.reset_paths(paths)
    try:
        store = RunStateStore(paths.agent_dir)
        store.create("run-terminal", agent_id="smith", session_id="session-1")
        store.transition("run-terminal", "running")
        store.transition("run-terminal", "completed")
        trace = TraceStore(paths.agent_dir)
        trace.append("run-terminal", ExecutionEvent(EventType.RUN_STARTED, {"run_id": "run-terminal"}))
        trace.append("run-terminal", ExecutionEvent(EventType.RUN_FINISHED, {
            "run_id": "run-terminal",
            "status": "completed",
        }))
        trace.seal("run-terminal")

        main._reconcile_startup_observability(store, recovered_run_ids=[])

        summary = RunSummaryStore(paths.agent_dir).get("run-terminal")
        assert summary is not None
        assert summary.summary.event_count == 2
        assert summary.summary.outcome == "completed"
    finally:
        main.common_config.reset_paths()


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
