from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.database import close_db  # noqa: E402
from common.paths import AppPaths  # noqa: E402
from engine.execution import EventType, ExecutionEvent, RunObservationContext, RunStateStore  # noqa: E402
from engine.observability import RunObservation, RunSummaryStore, TraceStore  # noqa: E402
from app import main  # noqa: E402
from app.infrastructure import auth  # noqa: E402
from app.infrastructure.database import get_app_db  # noqa: E402
from app.services.token_stats_service import TokenStatsService  # noqa: E402


@asynccontextmanager
async def _unused_backfill_connection():
    """Keep lifespan tests off the real ``~/.agent-smith`` database.

    ``_sync_token_stats`` opens a connection of its own before it reaches the
    service, so a test that only fakes the service would still touch the user's
    data directory.
    """
    yield None


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
        def __init__(self, db_provider=None, **_kwargs) -> None:
            self._db_provider = db_provider

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
    monkeypatch.setattr(main, "dedicated_connection", _unused_backfill_connection)

    with TestClient(main.app):
        assert token_path.is_file()
        assert token_path.read_text(encoding="utf-8").strip()


def test_server_lifespan_backfills_token_stats_without_blocking_requests(
    monkeypatch,
) -> None:
    """The token backfill is a background task, and shutdown drains it.

    It used to be awaited inside the lifespan, which kept the server from
    answering anything until it finished; the shell gives up on a backend that
    takes longer than 30s.  So "ran before the first request" is exactly the
    guarantee that was given up, and asserting it now only measures how many
    scheduler steps the fake happened to need.  What must hold instead: requests
    are served while it is still running, and it is not left dangling at
    shutdown — its private connection has to be closed with it.
    """
    started = threading.Event()
    release = threading.Event()
    outcome: list[str] = []
    closed: list[str] = []

    class FakeTokenStatsService:
        def __init__(self, db_provider=None, **_kwargs) -> None:
            self._db_provider = db_provider

        async def sync_from_traces(self) -> int:
            started.set()
            try:
                while not release.is_set():
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                outcome.append("cancelled")
                raise
            outcome.append("completed")
            return 0

        async def record_generation(self, record) -> None:
            return None

    @asynccontextmanager
    async def fake_dedicated_connection():
        try:
            yield "private-connection"
        finally:
            closed.append("closed")

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
    monkeypatch.setattr(main, "dedicated_connection", fake_dedicated_connection)

    try:
        with TestClient(main.app) as client:
            # Requests are answered while the backfill is deliberately stuck, and
            # serving them is also what waits for it to start — no assumption
            # about how many scheduler steps it needs to get there.
            deadline = time.monotonic() + 10.0
            while not started.is_set() and time.monotonic() < deadline:
                assert client.get("/api/health").status_code == 200
            assert started.is_set(), "the backfill task was never started"
            assert client.get("/api/health").status_code == 200
            assert outcome == []  # still running: shutdown has to deal with it
    finally:
        release.set()

    assert outcome == ["cancelled"]
    assert closed == ["closed"], "the backfill's own connection outlived the server"


@pytest.mark.asyncio
async def test_startup_backfill_failure_keeps_a_concurrent_request_write(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """P2 regression: a failed backfill must not erase a request's pending write.

    ``_sync_message_estimates`` selects the unpriced messages and then inserts an
    estimate per message.  A session deleted in between (the shell's ``/clear``
    is a ``DELETE /api/agent/sessions/{id}``) makes that insert violate the
    ``token_usage_events.session_id`` foreign key, and ``sync_from_traces``
    rolls back.  Run on the shared connection, that rollback discarded every
    other coroutine's uncommitted work too — and ``add_message`` executes its
    INSERT and commits it across an await boundary, so a user's message
    disappeared with no error raised on either side.
    """
    paths = AppPaths(data_dir=tmp_path / "data", project_root=tmp_path / "project")
    main.common_config.reset_paths(paths)
    try:
        request_db = await get_app_db()  # the one connection every request uses
        await request_db.execute(
            "INSERT INTO agent_profiles (id, name, role) VALUES ('agent-1','Smith','x')"
        )
        await request_db.execute("INSERT INTO sessions (id, agent_id) VALUES ('s1','agent-1')")
        await request_db.commit()

        async def failing_estimate_sync(self, db) -> int:
            # The request path is mid-flight: its INSERT has been executed and
            # its commit has not been reached yet.
            await request_db.execute(
                "INSERT INTO messages (id, session_id, role, content, created_at) "
                "VALUES ('u9','s1','user','keep me','2026-07-14T10:00:00+00:00')"
            )
            raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")

        monkeypatch.setattr(
            TokenStatsService, "_sync_message_estimates", failing_estimate_sync
        )

        await main._sync_token_stats()

        await request_db.commit()  # the request finishes its own write
        rows = await request_db.execute_fetchall("SELECT id FROM messages")
        assert [str(row["id"]) for row in rows] == ["u9"]
    finally:
        await close_db()
        main.common_config.reset_paths()


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
        def __init__(self, db_provider=None, **_kwargs) -> None:
            self._db_provider = db_provider

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
    monkeypatch.setattr(main, "dedicated_connection", _unused_backfill_connection)
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
        def __init__(self, db_provider=None, **_kwargs) -> None:
            self._db_provider = db_provider

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
    monkeypatch.setattr(main, "dedicated_connection", _unused_backfill_connection)
    monkeypatch.setattr(main, "RunStateStore", UnavailableRunStateStore)

    with TestClient(main.app):
        pass
