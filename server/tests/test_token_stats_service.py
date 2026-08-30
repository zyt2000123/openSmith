from __future__ import annotations

import asyncio
import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
import pytest_asyncio
from app import main
from app.infrastructure import schema as app_schema
from app.infrastructure.database import get_app_db
from app.infrastructure.repositories.session_repo import SessionRepo
from app.services import token_stats_service as token_stats_module
from app.services.token_stats_service import TokenStatsService
from common.database import close_db


@pytest_asyncio.fixture(autouse=True)
async def close_test_connections(monkeypatch: pytest.MonkeyPatch):
    """Ensure each in-memory aiosqlite worker stops before pytest exits."""
    connections: list[aiosqlite.Connection] = []
    connect = aiosqlite.connect

    async def tracked_connect(*args, **kwargs) -> aiosqlite.Connection:
        connection = await connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(aiosqlite, "connect", tracked_connect)
    yield
    for connection in connections:
        await connection.close()


@pytest.mark.asyncio
async def test_token_stats_aggregates_daily_models_and_streaks() -> None:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE token_usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            run_id TEXT,
            source_key TEXT,
            project_name TEXT NOT NULL DEFAULT '',
            project_path TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            occurred_at TEXT NOT NULL
        );
        INSERT INTO sessions (id, agent_id) VALUES ('s1', 'agent-1'), ('s2', 'agent-1');
        """
    )

    async def db_provider() -> aiosqlite.Connection:
        return db

    service = TokenStatsService(db_provider)
    await service.record_usage(
        session_id="s1",
        run_id="r1",
        project_name="Agent-Smith",
        project_path="/tmp/Agent-Smith",
        model="gpt-test",
        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        occurred_at=datetime.fromisoformat("2026-01-01T10:00:00+00:00"),
    )
    await service.record_usage(
        session_id="s1",
        run_id="r1",
        project_name="Agent-Smith",
        project_path="/tmp/Agent-Smith",
        model="gpt-test",
        usage={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
        occurred_at=datetime.fromisoformat("2026-01-02T11:00:00+00:00"),
    )
    await service.record_usage(
        session_id="s2",
        run_id="r2",
        project_name="Other",
        project_path="/tmp/Other",
        model="claude-test",
        usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
        occurred_at=datetime.fromisoformat("2026-01-04T14:00:00+00:00"),
    )
    await service.record_usage(
        session_id="s2",
        run_id="r3",
        project_name="Other",
        project_path="/tmp/Other",
        model="",
        usage={"input_tokens": 40, "output_tokens": 10, "total_tokens": 50},
        occurred_at=datetime.fromisoformat("2026-01-04T10:00:00+00:00"),
    )

    stats = await service.get_stats("agent-1", year=2026)

    assert stats["year"] == 2026
    assert stats["total_tokens"] == 98
    assert stats["input_tokens"] == 72
    assert stats["output_tokens"] == 26
    assert stats["session_count"] == 2
    assert stats["active_days"] == 3
    assert stats["current_streak"] == 1
    assert stats["longest_streak"] == 2
    assert stats["favorite_model"] == "gpt-test"
    assert stats["peak_hour"] == 10
    assert stats["daily"][0] == {
        "date": "2026-01-01",
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "sessions": 1,
    }
    assert stats["daily"][-1]["date"] == "2026-12-31"
    assert [model["model"] for model in stats["models"]] == ["gpt-test", "claude-test"]
    assert stats["models"][0]["total_tokens"] == 45


@pytest.mark.asyncio
async def test_record_usage_ignores_empty_usage() -> None:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(
        """
        CREATE TABLE token_usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            run_id TEXT,
            project_name TEXT NOT NULL DEFAULT '',
            project_path TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            occurred_at TEXT NOT NULL
        );
        """
    )

    async def db_provider() -> aiosqlite.Connection:
        return db

    service = TokenStatsService(db_provider)
    await service.record_usage(
        session_id="s1",
        run_id=None,
        project_name="",
        project_path="",
        model="",
        usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    )

    async with db.execute("SELECT count(*) AS count FROM token_usage_events") as cursor:
        row = await cursor.fetchone()
    assert row["count"] == 0


@pytest.mark.asyncio
async def test_sync_from_traces_imports_exact_usage_once(tmp_path: Path) -> None:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL
        );
        CREATE TABLE token_usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            run_id TEXT,
            source_key TEXT UNIQUE,
            project_name TEXT NOT NULL DEFAULT '',
            project_path TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            occurred_at TEXT NOT NULL
        );
        INSERT INTO sessions (id, agent_id) VALUES ('s1', 'agent-1');
        """
    )
    runs = tmp_path / "runs"
    traces = tmp_path / "traces"
    runs.mkdir()
    traces.mkdir()
    (runs / "run-1.json").write_text(
        json.dumps({"run_id": "run-1", "session_id": "s1"}),
        encoding="utf-8",
    )
    (traces / "run-1.jsonl").write_text(
        "\n".join(
            [
                json.dumps({
                    "seq": 1,
                    "timestamp": "2026-07-14T10:00:00+00:00",
                    "type": "run_started",
                    "data": {"project_path": "/tmp/demo-project"},
                }),
                json.dumps({
                    "seq": 2,
                    "timestamp": "2026-07-14T10:00:01+00:00",
                    "type": "raw_response_event",
                    "data": {
                        "type": "response.created",
                        "data": {"model": "gpt-test"},
                    },
                }),
                json.dumps({
                    "seq": 3,
                    "timestamp": "2026-07-14T10:00:02+00:00",
                    "type": "token_usage",
                    "data": {"input_tokens": 100, "output_tokens": 25, "total_tokens": 125},
                }),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    async def db_provider() -> aiosqlite.Connection:
        return db

    service = TokenStatsService(db_provider, trace_root=tmp_path)
    assert await service.sync_from_traces() == 1
    cursor_row = await db.execute_fetchall(
        """
        SELECT byte_offset, project_path, model
        FROM observability_trace_cursors
        WHERE run_id='run-1'
        """
    )
    assert len(cursor_row) == 1
    assert cursor_row[0]["byte_offset"] > 0
    assert cursor_row[0]["project_path"] == "/tmp/demo-project"
    assert cursor_row[0]["model"] == "gpt-test"

    with (traces / "run-1.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({
                "seq": 4,
                "timestamp": "2026-07-14T10:00:03+00:00",
                "type": "token_usage",
                "data": {"input_tokens": 40, "output_tokens": 10, "total_tokens": 50},
            })
            + "\n"
        )

    assert await service.sync_from_traces() == 1
    assert await service.sync_from_traces() == 0

    stats = await service.get_stats("agent-1", year=2026)
    assert stats["total_tokens"] == 175
    assert stats["favorite_model"] == "gpt-test"
    async with db.execute("SELECT project_name, project_path FROM token_usage_events") as cursor:
        row = await cursor.fetchone()
    assert row["project_name"] == "demo-project"
    assert row["project_path"] == "/tmp/demo-project"


@pytest.mark.asyncio
async def test_sync_from_messages_provides_explicit_local_estimate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, agent_id TEXT NOT NULL);
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE token_usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            run_id TEXT,
            source_key TEXT UNIQUE,
            project_name TEXT NOT NULL DEFAULT '',
            project_path TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            occurred_at TEXT NOT NULL
        );
        INSERT INTO sessions (id, agent_id) VALUES ('s1', 'agent-1');
        INSERT INTO messages (id, session_id, role, content, created_at)
        VALUES ('m1', 's1', 'user', 'hello world', '2026-07-14T10:00:00+00:00');
        INSERT INTO messages (id, session_id, role, content, created_at)
        VALUES ('m2', 's1', 'assistant', 'hello back', '2026-07-14T10:00:01+00:00');
        """
    )

    async def db_provider() -> aiosqlite.Connection:
        return db

    service = TokenStatsService(db_provider, trace_root=tmp_path)
    assert await service.sync_from_traces() == 2

    encoding_lookups = 0

    def track_encoding_lookup(_name: str):
        nonlocal encoding_lookups
        encoding_lookups += 1
        return SimpleNamespace(encode=lambda *_args, **_kwargs: [])

    monkeypatch.setitem(sys.modules, "tiktoken", SimpleNamespace(get_encoding=track_encoding_lookup))
    assert await service.sync_from_traces() == 0
    assert encoding_lookups == 0

    stats = await service.get_stats("agent-1", year=2026)
    assert stats["total_tokens"] > 0
    assert stats["estimated"] is True
    assert stats["models"] == []
    assert stats["favorite_model"] is None
    async with db.execute("SELECT count(*) AS count FROM token_usage_events") as cursor:
        row = await cursor.fetchone()
    assert row["count"] == 2


@pytest.mark.asyncio
async def test_sync_from_traces_skips_orphaned_sessions(tmp_path: Path) -> None:
    """Traces referencing deleted sessions must be skipped, not crash the sync."""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL
        );
        CREATE TABLE token_usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            run_id TEXT,
            source_key TEXT UNIQUE,
            project_name TEXT NOT NULL DEFAULT '',
            project_path TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            occurred_at TEXT NOT NULL
        );
        INSERT INTO sessions (id, agent_id) VALUES ('s1', 'agent-1');
        """
    )
    runs = tmp_path / "runs"
    traces = tmp_path / "traces"
    runs.mkdir()
    traces.mkdir()

    def _write_run(run_id: str, session_id: str) -> None:
        (runs / f"{run_id}.json").write_text(
            json.dumps({"run_id": run_id, "session_id": session_id}),
            encoding="utf-8",
        )
        (traces / f"{run_id}.jsonl").write_text(
            json.dumps({
                "seq": 1,
                "timestamp": "2026-07-14T10:00:00+00:00",
                "type": "token_usage",
                "data": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            }) + "\n",
            encoding="utf-8",
        )

    _write_run("run-live", "s1")
    _write_run("run-orphan", "ghost-session")

    async def db_provider() -> aiosqlite.Connection:
        return db

    service = TokenStatsService(db_provider, trace_root=tmp_path)
    assert await service.sync_from_traces() == 1

    rows = await db.execute_fetchall("SELECT run_id FROM token_usage_events")
    assert [row["run_id"] for row in rows] == ["run-live"]


@pytest.mark.asyncio
async def test_record_usage_clears_message_estimates_for_the_session() -> None:
    """Exact usage must supersede the local message estimates immediately, not
    only at the next sync_from_traces, or get_stats double-counts in between."""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, agent_id TEXT NOT NULL);
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE token_usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            run_id TEXT,
            source_key TEXT,
            project_name TEXT NOT NULL DEFAULT '',
            project_path TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            occurred_at TEXT NOT NULL
        );
        INSERT INTO sessions (id, agent_id) VALUES ('s1', 'agent-1'), ('s2', 'agent-1');
        INSERT INTO messages (id, session_id, role, content, created_at) VALUES
            ('m1', 's1', 'user', 'ask', '2026-07-14T10:00:00+00:00'),
            ('m2', 's1', 'assistant', 'answer', '2026-07-14T10:00:01+00:00'),
            ('m3', 's2', 'user', 'other', '2026-07-14T10:00:02+00:00');
        INSERT INTO token_usage_events
            (session_id, run_id, source_key, model, input_tokens, output_tokens, total_tokens, occurred_at)
        VALUES
            ('s1', NULL, 'message:m1', 'local-estimate', 5, 3, 8, '2026-07-14T10:00:00+00:00'),
            ('s1', NULL, 'message:m2', 'local-estimate', 1, 2, 3, '2026-07-14T10:00:01+00:00'),
            ('s2', NULL, 'message:m3', 'local-estimate', 9, 9, 18, '2026-07-14T10:00:02+00:00');
        """
    )

    async def db_provider() -> aiosqlite.Connection:
        return db

    service = TokenStatsService(db_provider)
    await service.record_usage(
        session_id="s1",
        run_id="r1",
        project_name="Agent-Smith",
        project_path="/tmp/Agent-Smith",
        model="gpt-test",
        usage={"input_tokens": 100, "output_tokens": 25, "total_tokens": 125},
        occurred_at=datetime.fromisoformat("2026-07-14T11:00:00+00:00"),
    )

    rows = await db.execute_fetchall(
        "SELECT session_id, source_key, total_tokens FROM token_usage_events ORDER BY session_id"
    )
    assert [dict(r) for r in rows] == [
        {"session_id": "s1", "source_key": None, "total_tokens": 125},
        {"session_id": "s2", "source_key": "message:m3", "total_tokens": 18},
    ]

    stats = await service.get_stats("agent-1", year=2026)
    assert stats["total_tokens"] == 143  # 125 exact + 18 estimate for s2, no double count


@pytest.mark.asyncio
async def test_sync_from_traces_skips_live_recorded_runs(tmp_path: Path) -> None:
    """S2 regression: a run whose usage was recorded live (source_key IS NULL)
    must not be re-imported from its trace, or every interactive run is counted
    twice after the first server restart."""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, agent_id TEXT NOT NULL);
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE token_usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            run_id TEXT,
            source_key TEXT,
            project_name TEXT NOT NULL DEFAULT '',
            project_path TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            occurred_at TEXT NOT NULL
        );
        INSERT INTO sessions (id, agent_id) VALUES ('s1', 'agent-1');
        """
    )
    runs = tmp_path / "runs"
    traces = tmp_path / "traces"
    runs.mkdir()
    traces.mkdir()
    (runs / "run-live.json").write_text(
        json.dumps({"run_id": "run-live", "session_id": "s1"}),
        encoding="utf-8",
    )
    (traces / "run-live.jsonl").write_text(
        "\n".join(
            [
                json.dumps({
                    "seq": 1,
                    "timestamp": "2026-07-14T10:00:00+00:00",
                    "type": "run_started",
                    "data": {"project_path": "/tmp/demo-project"},
                }),
                json.dumps({
                    "seq": 2,
                    "timestamp": "2026-07-14T10:00:01+00:00",
                    "type": "token_usage",
                    "data": {"input_tokens": 100, "output_tokens": 25, "total_tokens": 125},
                }),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    async def db_provider() -> aiosqlite.Connection:
        return db

    service = TokenStatsService(db_provider, trace_root=tmp_path)
    # Live path already recorded this run during its SSE stream.
    await service.record_usage(
        session_id="s1",
        run_id="run-live",
        project_name="demo-project",
        project_path="/tmp/demo-project",
        model="gpt-test",
        usage={"input_tokens": 100, "output_tokens": 25, "total_tokens": 125},
        occurred_at=datetime.fromisoformat("2026-07-14T10:00:01+00:00"),
    )

    # A restart imports traces; the live-recorded run must be skipped.
    assert await service.sync_from_traces() == 0

    rows = await db.execute_fetchall(
        "SELECT source_key, total_tokens FROM token_usage_events WHERE run_id='run-live'"
    )
    assert len(rows) == 1
    assert rows[0]["source_key"] is None
    assert rows[0]["total_tokens"] == 125

    stats = await service.get_stats("agent-1", year=2026)
    assert stats["total_tokens"] == 125


@pytest.mark.asyncio
async def test_record_usage_files_an_engine_estimate_as_a_local_estimate(
    tmp_path: Path,
) -> None:
    """P1 regression: a provider that reports no usage makes the engine
    synthesise one (usage_reported=0, ~3 tokens per CJK character).  Stored with
    a NULL source_key that guess reads back as provider billing data: get_stats
    cannot flag it, and the trace import counts the same turn a second time
    after a restart."""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, agent_id TEXT NOT NULL);
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE token_usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            run_id TEXT,
            source_key TEXT UNIQUE,
            project_name TEXT NOT NULL DEFAULT '',
            project_path TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            occurred_at TEXT NOT NULL
        );
        INSERT INTO sessions (id, agent_id) VALUES ('s1', 'agent-1');
        """
    )
    estimated_usage = {
        "input_tokens": 900,
        "output_tokens": 60,
        "total_tokens": 960,
        "usage_reported": 0,
        "estimated": True,
    }
    runs = tmp_path / "runs"
    traces = tmp_path / "traces"
    runs.mkdir()
    traces.mkdir()
    (runs / "run-est.json").write_text(
        json.dumps({"run_id": "run-est", "session_id": "s1"}),
        encoding="utf-8",
    )
    (traces / "run-est.jsonl").write_text(
        json.dumps({
            "seq": 1,
            "timestamp": "2026-07-14T10:00:00+00:00",
            "type": "token_usage",
            "data": estimated_usage,
        })
        + "\n",
        encoding="utf-8",
    )

    async def db_provider() -> aiosqlite.Connection:
        return db

    service = TokenStatsService(db_provider, trace_root=tmp_path)
    await service.record_usage(
        session_id="s1",
        run_id="run-est",
        project_name="demo-project",
        project_path="/tmp/demo-project",
        model="gpt-test",
        usage=estimated_usage,
        occurred_at=datetime.fromisoformat("2026-07-14T10:00:00+00:00"),
    )

    rows = await db.execute_fetchall("SELECT source_key FROM token_usage_events")
    assert len(rows) == 1
    assert str(rows[0]["source_key"]).startswith("estimate:live:")

    stats = await service.get_stats("agent-1", year=2026)
    assert stats["estimated"] is True
    assert stats["total_tokens"] == 960

    # A restart imports traces; the estimate is already recorded live, so
    # importing it again would count this turn twice.
    assert await service.sync_from_traces() == 0
    stats = await service.get_stats("agent-1", year=2026)
    assert stats["total_tokens"] == 960


@pytest.mark.asyncio
async def test_sync_from_traces_files_an_engine_estimate_as_a_local_estimate(
    tmp_path: Path,
) -> None:
    """Auto-task runs never call record_usage, so the trace import is the only
    path their usage takes; an estimated event must not land there as exact
    either."""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, agent_id TEXT NOT NULL);
        CREATE TABLE token_usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            run_id TEXT,
            source_key TEXT UNIQUE,
            project_name TEXT NOT NULL DEFAULT '',
            project_path TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            occurred_at TEXT NOT NULL
        );
        INSERT INTO sessions (id, agent_id) VALUES ('s1', 'agent-1');
        """
    )
    runs = tmp_path / "runs"
    traces = tmp_path / "traces"
    runs.mkdir()
    traces.mkdir()
    (runs / "run-auto.json").write_text(
        json.dumps({"run_id": "run-auto", "session_id": "s1"}),
        encoding="utf-8",
    )
    (traces / "run-auto.jsonl").write_text(
        "\n".join(
            [
                json.dumps({
                    "seq": 1,
                    "timestamp": "2026-07-14T10:00:00+00:00",
                    "type": "token_usage",
                    "data": {
                        "input_tokens": 900,
                        "output_tokens": 60,
                        "total_tokens": 960,
                        "usage_reported": 0,
                        "estimated": True,
                    },
                }),
                json.dumps({
                    "seq": 2,
                    "timestamp": "2026-07-14T10:00:05+00:00",
                    "type": "token_usage",
                    "data": {
                        "input_tokens": 100,
                        "output_tokens": 25,
                        "total_tokens": 125,
                        "usage_reported": 1,
                    },
                }),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    async def db_provider() -> aiosqlite.Connection:
        return db

    service = TokenStatsService(db_provider, trace_root=tmp_path)
    assert await service.sync_from_traces() == 2

    rows = await db.execute_fetchall(
        "SELECT source_key FROM token_usage_events ORDER BY occurred_at"
    )
    assert [row["source_key"] for row in rows] == [
        "estimate:trace:run-auto:1",
        "run-auto:2",
    ]

    stats = await service.get_stats("agent-1", year=2026)
    assert stats["estimated"] is True


@pytest.mark.asyncio
async def test_sync_from_traces_heals_legacy_double_count(tmp_path: Path) -> None:
    """S2 regression: rows imported from an older version that did not skip
    live-recorded runs are cleaned up so get_stats stops counting them twice."""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, agent_id TEXT NOT NULL);
        CREATE TABLE token_usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            run_id TEXT,
            source_key TEXT,
            project_name TEXT NOT NULL DEFAULT '',
            project_path TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            occurred_at TEXT NOT NULL
        );
        CREATE TABLE observability_trace_cursors (
            run_id TEXT PRIMARY KEY,
            byte_offset INTEGER NOT NULL DEFAULT 0,
            project_path TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO sessions (id, agent_id) VALUES ('s1', 'agent-1');
        INSERT INTO token_usage_events
            (session_id, run_id, source_key, model, input_tokens, output_tokens, total_tokens, occurred_at)
        VALUES
            ('s1', 'run-live', NULL, 'gpt-test', 100, 25, 125, '2026-07-14T10:00:01+00:00'),
            ('s1', 'run-live', 'run-live:2', 'gpt-test', 100, 25, 125, '2026-07-14T10:00:01+00:00');
        """
    )
    runs = tmp_path / "runs"
    traces = tmp_path / "traces"
    runs.mkdir()
    traces.mkdir()
    (runs / "run-live.json").write_text(
        json.dumps({"run_id": "run-live", "session_id": "s1"}),
        encoding="utf-8",
    )
    (traces / "run-live.jsonl").write_text(
        json.dumps({
            "seq": 2,
            "timestamp": "2026-07-14T10:00:01+00:00",
            "type": "token_usage",
            "data": {"input_tokens": 100, "output_tokens": 25, "total_tokens": 125},
        })
        + "\n",
        encoding="utf-8",
    )

    async def db_provider() -> aiosqlite.Connection:
        return db

    service = TokenStatsService(db_provider, trace_root=tmp_path)
    await service.sync_from_traces()

    rows = await db.execute_fetchall(
        "SELECT source_key, total_tokens FROM token_usage_events WHERE run_id='run-live'"
    )
    assert len(rows) == 1
    assert rows[0]["source_key"] is None

    stats = await service.get_stats("agent-1", year=2026)
    assert stats["total_tokens"] == 125


@pytest.mark.asyncio
async def test_record_usage_clears_own_trace_imported_rows(tmp_path: Path) -> None:
    """F4 regression: when a resumed run records usage live, any trace-imported
    rows for the same run must be dropped immediately instead of surviving for
    the whole process lifetime (get_stats would double-count until the next
    startup's sync_from_traces heals them)."""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, agent_id TEXT NOT NULL);
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE token_usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            run_id TEXT,
            source_key TEXT,
            project_name TEXT NOT NULL DEFAULT '',
            project_path TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            occurred_at TEXT NOT NULL
        );
        INSERT INTO sessions (id, agent_id) VALUES ('s1', 'agent-1');
        INSERT INTO token_usage_events
            (session_id, run_id, source_key, model, input_tokens, output_tokens, total_tokens, occurred_at)
        VALUES
            ('s1', 'run-resumed', 'run-resumed:2', 'gpt-test', 100, 25, 125, '2026-07-14T10:00:01+00:00');
        """
    )

    async def db_provider() -> aiosqlite.Connection:
        return db

    service = TokenStatsService(db_provider, trace_root=tmp_path)
    await service.record_usage(
        session_id="s1",
        run_id="run-resumed",
        project_name="demo-project",
        project_path="/tmp/demo-project",
        model="gpt-test",
        usage={"input_tokens": 100, "output_tokens": 25, "total_tokens": 125},
        occurred_at=datetime.fromisoformat("2026-07-14T10:00:01+00:00"),
    )

    rows = await db.execute_fetchall(
        "SELECT source_key FROM token_usage_events WHERE run_id='run-resumed'"
    )
    assert len(rows) == 1
    assert rows[0]["source_key"] is None

    stats = await service.get_stats("agent-1", year=2026)
    assert stats["total_tokens"] == 125


_TWO_HISTORICAL_TURNS_AND_A_LIVE_ONE = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, agent_id TEXT NOT NULL);
CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE token_usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    run_id TEXT,
    source_key TEXT UNIQUE,
    project_name TEXT NOT NULL DEFAULT '',
    project_path TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    occurred_at TEXT NOT NULL
);
INSERT INTO sessions (id, agent_id) VALUES ('s1', 'agent-1');
INSERT INTO messages (id, session_id, role, content, created_at) VALUES
    ('u1', 's1', 'user',      'first question ................', '2026-07-14T10:00:00+00:00'),
    ('a1', 's1', 'assistant', 'first answer ..................', '2026-07-14T10:00:01+00:00'),
    ('u2', 's1', 'user',      'second question ...............', '2026-07-14T10:10:00+00:00'),
    ('a2', 's1', 'assistant', 'second answer .................', '2026-07-14T10:10:01+00:00'),
    ('u3', 's1', 'user',      'third question ................', '2026-07-14T11:00:00+00:00');
"""

# The engine's own guess, emitted when the provider reported nothing at all.
_ENGINE_ESTIMATE = {
    "input_tokens": 900,
    "output_tokens": 60,
    "total_tokens": 960,
    "usage_reported": 0,
    "estimated": True,
}


@pytest.mark.asyncio
async def test_record_usage_keeps_estimates_of_turns_it_did_not_price(
    tmp_path: Path,
) -> None:
    """One engine estimate must not erase a whole conversation's transcript
    estimates.  Turns 1 and 2 were never priced (the relay reported no usage and
    predates the engine's fallback), so their 'message:' rows are the only
    record the panel has of them; only turn 3 — the one this event belongs to —
    is superseded."""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(
        _TWO_HISTORICAL_TURNS_AND_A_LIVE_ONE
        + """
        INSERT INTO token_usage_events
            (session_id, run_id, source_key, model, input_tokens, output_tokens, total_tokens, occurred_at)
        VALUES
            ('s1', NULL, 'message:u1', 'local-estimate', 100, 0, 100, '2026-07-14T10:00:00+00:00'),
            ('s1', NULL, 'message:a1', 'local-estimate', 0, 100, 100, '2026-07-14T10:00:01+00:00'),
            ('s1', NULL, 'message:u2', 'local-estimate', 100, 0, 100, '2026-07-14T10:10:00+00:00'),
            ('s1', NULL, 'message:a2', 'local-estimate', 0, 100, 100, '2026-07-14T10:10:01+00:00'),
            -- Left over from a sync that ran while this turn was already
            -- pending (server restarted mid-approval, run resumed afterwards).
            ('s1', NULL, 'message:u3', 'local-estimate', 100, 0, 100, '2026-07-14T11:00:00+00:00');
        """
    )

    async def db_provider() -> aiosqlite.Connection:
        return db

    service = TokenStatsService(db_provider, trace_root=tmp_path)
    await service.record_usage(
        session_id="s1",
        run_id="run-3",
        project_name="demo-project",
        project_path="/tmp/demo-project",
        model="gpt-test",
        usage=_ENGINE_ESTIMATE,
        occurred_at=datetime.fromisoformat("2026-07-14T11:00:03+00:00"),
    )

    rows = await db.execute_fetchall(
        "SELECT source_key FROM token_usage_events ORDER BY occurred_at"
    )
    keys = [row["source_key"] for row in rows]
    assert keys[:4] == ["message:u1", "message:a1", "message:u2", "message:a2"]
    # Turn 3 is priced now, so its transcript estimate is gone and not counted
    # a second time alongside the event that replaced it.
    assert "message:u3" not in keys
    assert len(keys) == 5 and str(keys[4]).startswith("estimate:live:")

    stats = await service.get_stats("agent-1", year=2026)
    assert stats["total_tokens"] == 400 + 960

    # The next startup's sync must not undo any of that: neither by deleting the
    # unpriced turns' estimates nor by rebuilding turn 3's.
    assert await service.sync_from_traces() == 0
    stats = await service.get_stats("agent-1", year=2026)
    assert stats["total_tokens"] == 400 + 960


@pytest.mark.asyncio
async def test_sync_rebuilds_estimates_only_for_turns_without_usage(
    tmp_path: Path,
) -> None:
    """Databases damaged by the session-wide delete are healed on the next sync,
    and the priced turn is still not double-counted while healing them."""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(
        _TWO_HISTORICAL_TURNS_AND_A_LIVE_ONE
        + """
        INSERT INTO messages (id, session_id, role, content, created_at) VALUES
            ('a3', 's1', 'assistant', 'third answer ..................', '2026-07-14T11:00:10+00:00');
        -- The turn-3 event survived; the four transcript estimates did not.
        INSERT INTO token_usage_events
            (session_id, run_id, source_key, model, input_tokens, output_tokens, total_tokens, occurred_at)
        VALUES
            ('s1', 'run-3', 'estimate:live:deadbeef', 'gpt-test', 900, 60, 960, '2026-07-14T11:00:03+00:00');
        """
    )

    async def db_provider() -> aiosqlite.Connection:
        return db

    service = TokenStatsService(db_provider, trace_root=tmp_path)
    assert await service.sync_from_traces() == 4

    rows = await db.execute_fetchall(
        "SELECT source_key FROM token_usage_events ORDER BY occurred_at"
    )
    assert [row["source_key"] for row in rows] == [
        "message:u1",
        "message:a1",
        "message:u2",
        "message:a2",
        "estimate:live:deadbeef",
    ]
    # u3/a3 are turn 3's messages; the live estimate already prices that turn.
    assert await service.sync_from_traces() == 0


# ``messages`` never loses rows outside a resume, and a resident Smith keeps one
# transcript store across every session, so tens of thousands of rows is the
# normal steady state rather than an extreme.  Both shapes matter and they fail
# differently: many sessions grow the table the startup pass walks, while one
# long session grows the *per-turn* term.  20x600 was the shape the previous
# version of this test used, and it is exactly the size at which the quadratic
# term is still invisible (22ms per turn on disk); a single session of a few
# thousand messages is where it shows (1.27s per LLM call at 4800).
_SCALE_SHAPES = ((20, 600), (1, 4800))

# Wall clock is the weaker of the checks below — it is here to catch an
# order-of-magnitude regression, not to measure anything, and an in-memory
# database is not the on-disk cost either way.  Measured here on 1x4800:
# 0.023s startup / 0.017s per turn now, against 2.34s / 2.30s before, so each
# bound has better than 10x headroom above the current cost and stays an order
# of magnitude below the cost it is guarding against.
_STARTUP_BUDGET_SECONDS = 5.0
_TURN_BUDGET_SECONDS = 0.25

# Every alias the transcript table is reachable under in these statements.
_TRANSCRIPT_SCAN = re.compile(r"^SCAN (?:m|b|messages)\b")

# The quadratic term never was a scan of ``messages``: it is the event lookup,
# a *SEARCH* on token_usage_events that only constrained ``session_id`` and so
# walked every one of that session's rows — almost all of which are the
# per-message estimates this statement is trying to clean up.  A check that only
# looks for scans of the transcript is structurally blind to it, so the event
# side is asserted directly: the seek must be bounded on both ends.
_EVENT_LOOKUP = re.compile(r"^(?:SCAN|SEARCH) e\b")


async def _plan(db: aiosqlite.Connection, sql: str, params: tuple = ()) -> list[str]:
    rows = await db.execute_fetchall("EXPLAIN QUERY PLAN " + sql, params)
    return [str(row["detail"]) for row in rows]


async def _transcript_rescans(
    db: aiosqlite.Connection, sql: str, params: tuple = ()
) -> list[str]:
    """Plan nodes that walk the transcript from inside a correlated subquery.

    Enumerating ``messages`` once at the top level is what the backfill is for;
    doing it again per candidate row of an enclosing loop is the quadratic
    defect, so the nesting — not the scan — is what this looks for.
    """
    rows = await db.execute_fetchall("EXPLAIN QUERY PLAN " + sql, params)
    nodes = {int(row["id"]): (int(row["parent"]), str(row["detail"])) for row in rows}

    def inside_subquery(node_id: int) -> bool:
        parent = nodes.get(node_id, (0, ""))[0]
        while parent:
            detail = nodes.get(parent, (0, ""))[1]
            if "SUBQUERY" in detail:
                return True
            parent = nodes.get(parent, (0, ""))[0]
        return False

    return [
        detail
        for node_id, (_parent, detail) in nodes.items()
        if _TRANSCRIPT_SCAN.match(detail) and inside_subquery(node_id)
    ]


async def _event_lookups(
    db: aiosqlite.Connection, sql: str, params: tuple = ()
) -> list[str]:
    return [
        detail
        for detail in await _plan(db, sql, params)
        if _EVENT_LOOKUP.match(detail)
    ]


async def _large_transcript_db(
    session_count: int, per_session: int
) -> aiosqlite.Connection:
    """A real-schema database holding a normal amount of accumulated history."""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await app_schema.ensure_schema(db)
    await db.execute(
        "INSERT INTO agent_profiles (id, name, role) VALUES ('agent-1', 'Smith', 'assistant')"
    )
    sessions, messages, estimates, events = [], [], [], []
    for session_index in range(session_count):
        session_id = f"s{session_index}"
        sessions.append((session_id, "agent-1"))
        for index in range(per_session):
            message_id = f"{session_id}-m{index}"
            role = "user" if index % 2 == 0 else "assistant"
            occurred_at = (
                f"2026-07-14T{index // 3600:02d}:{index // 60 % 60:02d}:{index % 60:02d}.000000+00:00"
            )
            messages.append((message_id, session_id, role, "x" * 200, occurred_at))
            estimates.append((session_id, f"message:{message_id}", occurred_at))
        # One priced turn per session: enough to make the turn test run for real
        # instead of short-circuiting on an event-free session.
        events.append((session_id, "2026-07-14T00:09:59.000000+00:00"))
    await db.executemany(
        "INSERT INTO sessions (id, agent_id) VALUES (?, ?)", sessions
    )
    await db.executemany(
        "INSERT INTO messages (id, session_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
        messages,
    )
    await db.executemany(
        "INSERT INTO token_usage_events "
        "(session_id, source_key, model, input_tokens, output_tokens, total_tokens, occurred_at) "
        "VALUES (?, ?, 'local-estimate', 50, 0, 50, ?)",
        estimates,
    )
    await db.executemany(
        "INSERT INTO token_usage_events "
        "(session_id, run_id, source_key, model, input_tokens, output_tokens, total_tokens, occurred_at) "
        "VALUES (?, 'run-old', NULL, 'gpt-test', 100, 25, 125, ?)",
        events,
    )
    await db.commit()
    return db


@pytest.mark.parametrize(("session_count", "per_session"), _SCALE_SHAPES)
@pytest.mark.asyncio
async def test_turn_scoped_cleanup_stays_index_bound_on_an_accumulated_transcript(
    tmp_path: Path,
    session_count: int,
    per_session: int,
) -> None:
    """P1 regression: the turn-scoped cleanup must not cost O(messages-per-session^2).

    The cleanup asks, per candidate estimate, "does this session hold a usage
    event of the same turn?".  Phrased as "no user message lies between them",
    the only thing bounding the event side is ``e.session_id``, so it walked
    every event of the session — and a session's events are one per message.
    Doubling a session's transcript therefore quadrupled the cost of *every*
    LLM call, which runs inside the SSE stream: 20x2400 measured 324ms per
    call, one session of 9600 measured 5.3s per call, on disk.
    """
    db = await _large_transcript_db(session_count, per_session)

    async def db_provider() -> aiosqlite.Connection:
        return db

    for label, sql, params in (
        ("startup delete", token_stats_module._SUPERSEDED_ESTIMATES_DELETE, ()),
        (
            "per-turn delete",
            token_stats_module._SUPERSEDED_ESTIMATES_DELETE_FOR_SESSION,
            ("s0",),
        ),
        ("startup select", token_stats_module._UNPRICED_TRANSCRIPT_MESSAGES, ()),
        ("orphan delete", token_stats_module._ORPHANED_ESTIMATES_DELETE, ()),
    ):
        assert not await _transcript_rescans(db, sql, params), (
            label,
            await _plan(db, sql, params),
        )

    # The turn test seeks a timestamp range through the composite index; without
    # it, it falls back to walking every message of the session.
    for sql in (
        token_stats_module._SUPERSEDED_ESTIMATES_DELETE,
        token_stats_module._UNPRICED_TRANSCRIPT_MESSAGES,
    ):
        plan = await _plan(db, sql)
        assert any("idx_messages_session_role_time" in line for line in plan), plan

    # The event lookup is the quadratic term, so it is asserted where it lives:
    # every statement that runs it must bound ``occurred_at`` on both sides, not
    # just pin the session.
    for label, sql, params in (
        ("startup delete", token_stats_module._SUPERSEDED_ESTIMATES_DELETE, ()),
        (
            "per-turn delete",
            token_stats_module._SUPERSEDED_ESTIMATES_DELETE_FOR_SESSION,
            ("s0",),
        ),
        ("startup select", token_stats_module._UNPRICED_TRANSCRIPT_MESSAGES, ()),
    ):
        lookups = await _event_lookups(db, sql, params)
        assert lookups, (label, await _plan(db, sql, params))
        for detail in lookups:
            assert "idx_token_usage_session_time" in detail, (label, detail)
            assert "occurred_at>" in detail and "occurred_at<" in detail, (label, detail)

    # The hot path binds a session and must actually narrow by it.
    plan = await _plan(
        db, token_stats_module._SUPERSEDED_ESTIMATES_DELETE_FOR_SESSION, ("s0",)
    )
    assert any(
        line.startswith("SEARCH token_usage_events USING INDEX")
        and "session_id=?" in line
        for line in plan
    ), plan

    service = TokenStatsService(db_provider, trace_root=tmp_path)
    started = time.perf_counter()
    assert await service.sync_from_traces() == 0
    startup_seconds = time.perf_counter() - started
    assert startup_seconds < _STARTUP_BUDGET_SECONDS, startup_seconds

    started = time.perf_counter()
    await service.record_usage(
        session_id="s0",
        run_id="run-live",
        project_name="demo-project",
        project_path="/tmp/demo-project",
        model="gpt-test",
        usage={"input_tokens": 100, "output_tokens": 25, "total_tokens": 125},
        occurred_at=datetime.fromisoformat("2026-07-14T00:09:58+00:00"),
    )
    turn_seconds = time.perf_counter() - started
    assert turn_seconds < _TURN_BUDGET_SECONDS, turn_seconds


@pytest.mark.asyncio
async def test_sync_clears_estimates_whose_message_was_discarded(
    tmp_path: Path,
) -> None:
    """P3 regression: an estimate outlives the message row it was derived from.

    Resuming an interrupted run deletes the assistant messages after the resumed
    user turn (``discard_assistant_messages_after_user``).  The turn-scoped
    delete reaches an estimate *through* its message row, so once that row is
    gone the estimate can never be matched again: one leaks per resume, and a
    single leftover is enough to mark the whole panel ``estimated``.
    """
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, agent_id TEXT NOT NULL);
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE token_usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            run_id TEXT,
            source_key TEXT UNIQUE,
            project_name TEXT NOT NULL DEFAULT '',
            project_path TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            occurred_at TEXT NOT NULL
        );
        INSERT INTO sessions (id, agent_id) VALUES ('s1', 'agent-1');
        -- 'a1' is the half-finished assistant reply the resume threw away; only
        -- the user message it answered is still in the transcript.
        INSERT INTO messages (id, session_id, role, content, created_at) VALUES
            ('u1', 's1', 'user', 'ask', '2026-07-14T10:00:00+00:00');
        INSERT INTO token_usage_events
            (session_id, run_id, source_key, model, input_tokens, output_tokens, total_tokens, occurred_at)
        VALUES
            ('s1', NULL, 'message:u1', 'local-estimate', 5, 0, 5, '2026-07-14T10:00:00+00:00'),
            ('s1', NULL, 'message:a1', 'local-estimate', 0, 7, 7, '2026-07-14T10:00:01+00:00'),
            ('s1', 'run-1', NULL, 'gpt-test', 100, 25, 125, '2026-07-14T10:00:05+00:00');
        """
    )

    async def db_provider() -> aiosqlite.Connection:
        return db

    service = TokenStatsService(db_provider, trace_root=tmp_path)
    assert await service.sync_from_traces() == 0

    rows = await db.execute_fetchall("SELECT source_key FROM token_usage_events")
    assert [row["source_key"] for row in rows] == [None]

    stats = await service.get_stats("agent-1", year=2026)
    assert stats["estimated"] is False
    assert stats["total_tokens"] == 125

    # Nothing is regenerated for the discarded message on the next startup.
    assert await service.sync_from_traces() == 0
    rows = await db.execute_fetchall("SELECT source_key FROM token_usage_events")
    assert [row["source_key"] for row in rows] == [None]


class _FailsAfterNExecutes:
    """A connection proxy that stops working part-way through the backfill."""

    def __init__(self, db: aiosqlite.Connection, limit: int) -> None:
        self._db = db
        self._limit = limit
        self.calls = 0

    def __getattr__(self, name: str):
        return getattr(self._db, name)

    async def execute(self, *args, **kwargs):
        self.calls += 1
        if self.calls > self._limit:
            raise aiosqlite.IntegrityError("FOREIGN KEY constraint failed")
        return await self._db.execute(*args, **kwargs)


@pytest.mark.asyncio
async def test_backfill_commits_incrementally_instead_of_one_long_transaction(
    tmp_path: Path,
) -> None:
    """The estimate backfill must not hold one write transaction over the whole
    transcript.

    Now that it runs on its own connection it is a second writer, and SQLite
    grants the write lock to one connection at a time, so a transaction that
    spans the whole backfill locks the request path out of the database for its
    whole duration.  That consequence is covered by
    ``test_startup_backfill_never_locks_out_a_concurrent_request_write``; what
    is asserted here is the other observable half — a backfill which dies
    part-way leaves its finished batches committed, visible to another
    connection, so the next startup resumes rather than restarts.

    Deliberately not asserted: *which* batch boundary the crash landed after.
    The commit criterion is a time budget with a row count as a second ceiling,
    so the boundary moves with how fast the machine is; pinning it to
    ``_ESTIMATE_COMMIT_INTERVAL`` would turn a slower CI container into a
    failure while the invariant still held.
    """
    db_path = tmp_path / "app.db"
    writer = await aiosqlite.connect(str(db_path))
    writer.row_factory = aiosqlite.Row
    await writer.execute("PRAGMA journal_mode=WAL")
    await app_schema.ensure_schema(writer)
    await writer.execute(
        "INSERT INTO agent_profiles (id, name, role) VALUES ('agent-1', 'Smith', 'assistant')"
    )
    await writer.execute("INSERT INTO sessions (id, agent_id) VALUES ('s1', 'agent-1')")
    total = 3 * token_stats_module._ESTIMATE_COMMIT_INTERVAL
    await writer.executemany(
        "INSERT INTO messages (id, session_id, role, content, created_at) VALUES (?, 's1', 'user', 'ask', ?)",
        [
            (f"m{index}", f"2026-07-14T{index // 3600:02d}:{index // 60 % 60:02d}:{index % 60:02d}+00:00")
            for index in range(total)
        ],
    )
    await writer.commit()

    # Two leading statements (the superseded and orphan deletes) precede the
    # per-message inserts.  The row ceiling puts a boundary at or before 500, so
    # dying here always lands after at least one committed batch and before the
    # last one, whichever gate actually fired.
    proxy = _FailsAfterNExecutes(writer, limit=2 + token_stats_module._ESTIMATE_COMMIT_INTERVAL + 17)

    async def failing_provider():
        return proxy

    service = TokenStatsService(failing_provider, trace_root=tmp_path / "no-traces")
    with pytest.raises(aiosqlite.IntegrityError):
        await service.sync_from_traces()

    reader = await aiosqlite.connect(str(db_path))
    reader.row_factory = aiosqlite.Row
    rows = await reader.execute_fetchall("SELECT count(*) AS n FROM token_usage_events")
    committed = int(rows[0]["n"])
    # Some batches survived the crash (not one transaction over the whole run)
    # and not all of them did (the crash really was mid-backfill).
    assert 0 < committed < total

    # The next startup picks up exactly what is missing, not the whole transcript.
    async def healthy_provider():
        return writer

    resumed = TokenStatsService(healthy_provider, trace_root=tmp_path / "no-traces")
    assert await resumed.sync_from_traces() == total - committed

    # And the two runs together priced every message exactly once — a resumed
    # backfill must neither duplicate the committed prefix nor skip it.
    rows = await reader.execute_fetchall(
        "SELECT count(*) AS n, count(DISTINCT source_key) AS distinct_keys FROM token_usage_events"
    )
    assert int(rows[0]["n"]) == total
    assert int(rows[0]["distinct_keys"]) == total


# A transcript turn, not a token: prose, a code line and a hash, so the
# tokeniser is billed what a real message costs it rather than what ``'ask'``
# costs it.  The sibling test above runs on 3-character rows, and at that size
# a row-counted batch looks bounded no matter how long it actually holds the
# lock — which is exactly the blind spot this test exists to cover.
_TRANSCRIPT_TURN_BYTES = 2048
# 20k turns of that size is the transcript the defect was reported on, and the
# smallest scale that makes a *row*-counted backfill hold the writer lock past
# the request path's 5s ``busy_timeout``: measured on the pre-fix loop, one
# request write waited 5.1s of a 5.7s backfill and failed, while the fixed loop
# leaves the same write waiting 0.8s.  A smaller transcript only demotes that
# to "waited 3s and survived", which is the reading that let the regression in.
# It costs ~7s, most of it the backfill itself — the fix bounds the lock hold,
# not the total work, so the run is no faster once it passes.
_TRANSCRIPT_TURNS = 20_000


def _transcript_turn(nbytes: int) -> str:
    """One deterministic ~``nbytes`` message shaped like real transcript text."""
    rng = random.Random(20260830)
    chunks: list[str] = []
    size = 0
    while size < nbytes:
        kind = rng.randrange(3)
        if kind == 0:
            chunk = " ".join(
                rng.choice(
                    [
                        "the startup backfill holds the single writer lock",
                        "回填在写事务里做编码, 请求侧只能干等",
                        "add_message waited on busy_timeout and then failed",
                    ]
                )
                for _ in range(2)
            )
        elif kind == 1:
            chunk = (
                f'    cursor = await db.execute(_INSERT_ESTIMATE, (row["session_id"], '
                f'"message:{rng.randrange(10 ** 6)}", token_count))'
            )
        else:
            chunk = "blob " + "".join(rng.choice("0123456789abcdef") for _ in range(56))
        chunks.append(chunk)
        size += len(chunk.encode()) + 1
    return "\n".join(chunks)


@pytest.mark.asyncio
async def test_startup_backfill_never_locks_out_a_concurrent_request_write() -> None:
    """A user sending a message during the startup backfill must not lose it.

    The invariant: while ``_sync_token_stats`` runs on its own connection, a
    request-path ``add_message`` on the shared connection still completes —
    zero ``OperationalError``, every message on disk.

    Why that needs its own test.  The backfill is a second writer, and SQLite
    grants the write lock to one connection at a time.  A *row*-counted commit
    interval bounds how many rows a transaction covers, not how long it holds
    the lock: the per-row cost is dominated by tokenising the message, so the
    same 500 rows hold the lock for milliseconds on ``'ask'`` and for most of a
    second on real turns.  The waiting writer does not win the sub-millisecond
    gap between one COMMIT and the next INSERT — measured here, it waits out
    the *whole* backfill — so once the backfill outlasts the 5s
    ``busy_timeout`` the request's INSERT raises ``database is locked``.  That
    surfaces to the user as "执行失败", and the turn is lost: the user message
    never reached the database.

    Both assertions are on semantic facts rather than on how long anything
    took.  Nothing here asserts a deadline for the backfill, because a deadline
    can be widened until it stops meaning anything; a lost message and a
    request that had to queue behind the entire backfill cannot be.
    """
    turn = _transcript_turn(_TRANSCRIPT_TURN_BYTES)
    request_db = await get_app_db()  # the one connection every request uses
    try:
        await request_db.execute(
            "INSERT INTO agent_profiles (id, name, role) VALUES ('agent-1','Smith','x')"
        )
        await request_db.execute("INSERT INTO sessions (id, agent_id) VALUES ('s1','agent-1')")
        await request_db.executemany(
            "INSERT INTO messages (id, session_id, role, content, created_at) "
            "VALUES (?, 's1', 'user', ?, ?)",
            [
                (
                    f"seed-{index}",
                    turn,
                    f"2026-07-14T{index // 3600 % 24:02d}:{index // 60 % 60:02d}:{index % 60:02d}+00:00",
                )
                for index in range(_TRANSCRIPT_TURNS)
            ],
        )
        await request_db.commit()

        repo = SessionRepo()
        backfill_done = asyncio.Event()
        backfill_seconds = 0.0
        locked_out: list[str] = []
        waits: list[float] = []
        sent = 0

        async def backfill() -> None:
            nonlocal backfill_seconds
            started = time.monotonic()
            await main._sync_token_stats()
            backfill_seconds = time.monotonic() - started
            backfill_done.set()

        async def keep_sending() -> None:
            """The user keeps talking to Smith while the panel backfills."""
            nonlocal sent
            while not backfill_done.is_set():
                started = time.monotonic()
                try:
                    await repo.add_message("s1", "user", f"live turn {sent}")
                except aiosqlite.OperationalError as exc:
                    locked_out.append(str(exc))
                waits.append(time.monotonic() - started)
                sent += 1
                await asyncio.sleep(0.05)

        await asyncio.gather(backfill(), keep_sending())
        assert backfill_seconds > 0 and waits

        # 1. The request path never saw the lock.
        assert locked_out == []

        # 2. Every message the user sent is on disk.  The count is the point:
        #    the failure mode is a turn that vanishes, and an INSERT that raised
        #    left nothing behind.
        rows = await request_db.execute_fetchall(
            "SELECT count(*) AS n FROM messages WHERE content LIKE 'live turn %'"
        )
        assert int(rows[0]["n"]) == sent

        # 3. No single request write was queued behind the whole backfill.
        #    Deliberately a ratio and not a deadline: a deadline is satisfied by
        #    widening it, while both sides of this scale together, so a slower
        #    machine (or a bigger transcript) does not move it.  Serialising the
        #    request path behind the backfill puts one write at ~1.0; batching
        #    that actually bounds the hold keeps every write far below it.
        #    This is what still fails on a machine fast enough to drain the
        #    backfill inside the 5s ``busy_timeout``, where assertion 1 would
        #    pass by luck rather than by the invariant holding.
        assert max(waits) * 2 < backfill_seconds, (
            f"a request write waited {max(waits):.2f}s of a {backfill_seconds:.2f}s "
            f"backfill ({sent} sent); the request path was queued behind it"
        )

        # 4. The backfill really did the work.  ``_sync_token_stats`` swallows
        #    its own exceptions, so without this a backfill that died on its
        #    first statement would satisfy everything above.
        rows = await request_db.execute_fetchall(
            "SELECT count(*) AS n FROM messages m WHERE m.id LIKE 'seed-%' AND NOT EXISTS ("
            "  SELECT 1 FROM token_usage_events e WHERE e.source_key = 'message:' || m.id)"
        )
        assert int(rows[0]["n"]) == 0
    finally:
        await close_db()


@pytest.mark.asyncio
async def test_schema_upgrade_adds_the_turn_index_to_a_populated_database() -> None:
    """The index has to reach installs that already hold history, without
    disturbing it — an existing ``~/.agent-smith/app.db`` is the only copy of a
    resident Smith's transcripts."""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    # A database at the previous schema: everything except the new index.
    await db.executescript(
        app_schema.APP_SCHEMA.replace("messages(session_id, role, created_at)", "messages(session_id)")
    )
    await db.execute(
        "INSERT INTO agent_profiles (id, name, role) VALUES ('agent-1', 'Smith', 'assistant')"
    )
    await db.execute("INSERT INTO sessions (id, agent_id) VALUES ('s1', 'agent-1')")
    await db.execute(
        "INSERT INTO messages (id, session_id, role, content, created_at) "
        "VALUES ('u1', 's1', 'user', 'ask', '2026-07-14T10:00:00+00:00')"
    )
    await db.commit()

    await app_schema.ensure_schema(db)

    indexes = {
        str(row["name"])
        for row in await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='messages'"
        )
    }
    assert "idx_messages_session_role_time" in indexes
    rows = await db.execute_fetchall("SELECT id, created_at FROM messages")
    assert [dict(row) for row in rows] == [
        {"id": "u1", "created_at": "2026-07-14T10:00:00+00:00"}
    ]
    # Idempotent: a second startup on the upgraded database is a no-op.
    await app_schema.ensure_schema(db)
    assert len(await db.execute_fetchall("SELECT id FROM messages")) == 1

