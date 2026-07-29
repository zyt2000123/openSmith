from __future__ import annotations

from datetime import datetime, timezone
import importlib

import aiosqlite
import pytest

from app.infrastructure import schema as schema_module
from app.infrastructure.repositories.auto_task_repo import AutoTaskRepo


@pytest.mark.asyncio
async def test_auto_task_schema_migrates_retry_lease_and_working_directory_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(
        """
        CREATE TABLE auto_tasks (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            trigger_type TEXT NOT NULL DEFAULT 'manual',
            trigger_config TEXT NOT NULL DEFAULT '',
            instruction TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'idle',
            last_run_at TEXT,
            next_run_at TEXT,
            run_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        """
    )

    await schema_module.ensure_schema(db)
    async with db.execute("PRAGMA table_info(auto_tasks)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}

    assert {
        "working_dir",
        "retry_count",
        "max_retries",
        "lease_until",
        "lease_token",
    } <= columns
    await db.close()


@pytest.mark.asyncio
async def test_auto_task_claim_uses_a_lease_to_prevent_duplicate_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await schema_module.ensure_schema(db)
    repo_module = importlib.import_module("app.infrastructure.repositories.auto_task_repo")

    async def fake_get_app_db():
        return db

    monkeypatch.setitem(repo_module.AutoTaskRepo.create.__globals__, "get_app_db", fake_get_app_db)
    repo = AutoTaskRepo()
    await repo.create(
        "smith",
        {
            "title": "probe",
            "instruction": "check",
            "working_dir": "/tmp/project",
            "trigger_type": "interval",
            "trigger_config": "60",
            "next_run_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    task = (await repo.list_by_agent("smith"))[0]

    lease_token = await repo.claim_running(task["id"])
    assert lease_token is not None
    assert await repo.claim_running(task["id"]) is None
    claimed = await repo.get(task["id"])
    assert claimed is not None
    assert claimed["lease_until"] is not None
    assert claimed["lease_token"] == lease_token
    assert await repo.renew_lease(task["id"], lease_token) is True
    assert await repo.finish_task(task["id"], "idle", None, lease_token, retry_count=0) is True
    assert await repo.finish_task(task["id"], "idle", None, lease_token, retry_count=99) is False

    await db.close()


@pytest.mark.asyncio
async def test_stale_lease_cannot_overwrite_retry_state_after_reclaim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await schema_module.ensure_schema(db)
    repo_module = importlib.import_module("app.infrastructure.repositories.auto_task_repo")

    async def fake_get_app_db():
        return db

    monkeypatch.setitem(repo_module.AutoTaskRepo.create.__globals__, "get_app_db", fake_get_app_db)
    repo = AutoTaskRepo()
    task = await repo.create(
        "smith",
        {
            "title": "probe",
            "instruction": "check",
            "working_dir": "/tmp/project",
            "retry_count": 2,
        },
    )
    first_lease = await repo.claim_running(task["id"])
    assert first_lease is not None
    await db.execute(
        "UPDATE auto_tasks SET lease_until=? WHERE id=?",
        ("2000-01-01T00:00:00+00:00", task["id"]),
    )
    await db.commit()
    second_lease = await repo.claim_running(task["id"])
    assert second_lease is not None and second_lease != first_lease

    assert await repo.finish_task(
        task["id"],
        "idle",
        None,
        first_lease,
        retry_count=99,
    ) is False
    current = await repo.get(task["id"])
    assert current is not None
    assert current["retry_count"] == 2
    assert current["lease_token"] == second_lease

    await db.close()
