from __future__ import annotations

from datetime import datetime, timedelta, timezone
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


@pytest.mark.asyncio
async def test_auto_task_working_dir_round_trips_through_all_read_paths(
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
            "trigger_type": "interval",
            "trigger_config": "60",
            "next_run_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    # The scheduler reads tasks via _row_to_dict (get/list_by_agent/list_due_tasks);
    # losing working_dir there made every auto task fail with "no working directory".
    assert task["working_dir"] == "/tmp/project"
    assert (await repo.get(task["id"]))["working_dir"] == "/tmp/project"
    assert (await repo.list_by_agent("smith"))[0]["working_dir"] == "/tmp/project"
    assert (await repo.list_due_tasks())[0]["working_dir"] == "/tmp/project"

    await repo.update(task["id"], {"working_dir": "/tmp/other"})
    assert (await repo.get(task["id"]))["working_dir"] == "/tmp/other"

    await db.close()


@pytest.mark.asyncio
async def test_schema_migrates_legacy_space_timestamps_to_iso_format() -> None:
    """Rows written by the old datetime('now') default (space separator) must be
    normalized to the T-separated format on startup so TEXT ordering stays sane."""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await schema_module.ensure_schema(db)
    await db.execute(
        "INSERT INTO sessions (id, agent_id, created_at) VALUES (?, ?, ?)",
        ("legacy", "smith", "2026-07-20 12:00:00"),
    )
    await db.execute(
        "INSERT INTO sessions (id, agent_id, created_at) VALUES (?, ?, ?)",
        ("modern", "smith", "2026-07-20T12:00:00+00:00"),
    )
    await db.commit()

    await schema_module.ensure_schema(db)

    rows = await db.execute_fetchall("SELECT id, created_at FROM sessions ORDER BY id")
    assert dict(rows[0]) == {"id": "legacy", "created_at": "2026-07-20T12:00:00"}
    assert dict(rows[1]) == {"id": "modern", "created_at": "2026-07-20T12:00:00+00:00"}

    # Idempotent: a second pass must not touch the already-normalized rows.
    await schema_module.ensure_schema(db)
    rows = await db.execute_fetchall("SELECT id, created_at FROM sessions ORDER BY id")
    assert dict(rows[0])["created_at"] == "2026-07-20T12:00:00"

    await db.close()


@pytest.mark.asyncio
async def test_startup_reset_only_reclaims_expired_leases() -> None:
    """ensure_schema must not steal a *live* lease from a running worker.

    The shared DB can be opened by several processes (server workers, CLI,
    dev-reloads); resetting a live lease lets a second process reclaim a task
    that is still executing, running the same instruction twice.
    """
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await schema_module.ensure_schema(db)
    now = datetime.now(timezone.utc).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    await db.execute(
        "INSERT INTO auto_tasks (id, agent_id, title, description, trigger_type, "
        "trigger_config, instruction, enabled, status, next_run_at, created_at, "
        "working_dir, lease_until, lease_token) VALUES "
        "('live', 'a', 't', '', 'manual', '', 'x', 1, 'running', NULL, ?, ?, ?, ?), "
        "('expired', 'a', 't', '', 'manual', '', 'x', 1, 'running', NULL, ?, ?, ?, ?)",
        (now, "wd", future, "live-token", now, "wd", past, "old-token"),
    )
    await db.commit()

    # A second process booting now must leave the live lease untouched.
    await schema_module.ensure_schema(db)

    async with db.execute("SELECT id, status, lease_token FROM auto_tasks ORDER BY id") as cursor:
        rows = {row["id"]: row for row in await cursor.fetchall()}
    assert rows["live"]["status"] == "running"
    assert rows["live"]["lease_token"] == "live-token"
    assert rows["expired"]["status"] == "idle"
    assert rows["expired"]["lease_token"] is None
    await db.close()


@pytest.mark.asyncio
async def test_finish_run_is_gated_on_the_owning_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker that lost its lease must not record a stale run row."""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await schema_module.ensure_schema(db)
    repo_module = importlib.import_module("app.infrastructure.repositories.auto_task_repo")

    async def fake_get_app_db():
        return db

    monkeypatch.setitem(repo_module.AutoTaskRepo.create.__globals__, "get_app_db", fake_get_app_db)
    repo = AutoTaskRepo()

    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO auto_tasks (id, agent_id, title, description, trigger_type, "
        "trigger_config, instruction, enabled, status, next_run_at, created_at, "
        "working_dir, lease_until, lease_token) VALUES "
        "('task-1', 'a', 't', '', 'manual', '', 'x', 1, 'running', NULL, ?, ?, ?, ?)",
        (now, "wd", now, "current-token"),
    )
    run = await repo.create_run("task-1")
    await db.commit()

    # The current owner records its run fine...
    finished = await repo.finish_run(
        run["id"], "completed", "ok", auto_task_id="task-1", lease_token="current-token"
    )
    assert finished is not None
    # ...but a superseded worker (wrong token) cannot overwrite it.
    await db.execute(
        "UPDATE auto_tasks SET status='running', lease_token='new-token', lease_until=? "
        "WHERE id='task-1'",
        (now,),
    )
    await db.commit()
    stale = await repo.finish_run(
        run["id"], "completed", "stale", auto_task_id="task-1", lease_token="old-token"
    )
    assert stale is None
    await db.close()


@pytest.mark.asyncio
async def test_lease_claim_is_atomic_under_concurrent_contention(monkeypatch) -> None:
    """Ten concurrent workers claiming one task must yield exactly one winner.

    claim_running's atomic UPDATE ... WHERE lease guard is the fencing primitive:
    under real concurrency, a second worker must never observe the task as
    claimable while the first still holds a live lease.
    """
    import asyncio
    import importlib

    import aiosqlite

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

    tokens = await asyncio.gather(*[repo.claim_running(task["id"]) for _ in range(10)])
    winners = [token for token in tokens if token is not None]
    assert len(winners) == 1

    # The superseded workers cannot renew or finish the lease they never won.
    assert await repo.renew_lease(task["id"], "not-the-winner") is False
    assert await repo.finish_task(task["id"], "idle", None, "not-the-winner") is False
    # A second claim while the winner holds a live lease still fails.
    assert await repo.claim_running(task["id"]) is None

    # After the winner releases, the task is claimable again.
    assert await repo.finish_task(task["id"], "idle", None, winners[0]) is True
    assert await repo.claim_running(task["id"]) is not None
    await db.close()


@pytest.mark.asyncio
async def test_lease_expiry_reclaim_is_fenced_by_token(monkeypatch) -> None:
    """A lease that expired is re-claimable, but the superseded worker's token
    can never release the new holder's lease (fencing by token, not by time)."""
    import importlib

    import aiosqlite

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

    old_worker = await repo.claim_running(task["id"])
    assert old_worker is not None

    # Simulate the 15-minute lease lapsing (worker A stalls past expiry).
    past = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
    await db.execute(
        "UPDATE auto_tasks SET lease_until=? WHERE id=?",
        (past, task["id"]),
    )
    await db.commit()

    # Worker B reclaims the expired lease.
    new_worker = await repo.claim_running(task["id"])
    assert new_worker is not None
    assert new_worker != old_worker

    # A (superseded) cannot renew or release B's lease; only B's token works.
    assert await repo.renew_lease(task["id"], old_worker) is False
    assert await repo.finish_task(task["id"], "idle", None, old_worker) is False
    assert await repo.renew_lease(task["id"], new_worker) is True
    assert await repo.finish_task(task["id"], "idle", None, new_worker) is True
    await db.close()
