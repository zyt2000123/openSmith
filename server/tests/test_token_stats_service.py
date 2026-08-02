from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
import pytest_asyncio
from app.services.token_stats_service import TokenStatsService


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
