"""Switching a scheduled task to ``manual`` must actually stop it.

``update_auto_task`` computes ``next_run_at = None`` for a manual trigger, but
the repo's UPDATE skipped every field whose value was ``None`` — so the stale
timestamp survived, and ``list_due_tasks`` does not filter on
``trigger_type``.  The scheduler then claimed the task one more time and ran a
full unattended engine turn after the user had disabled it.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from app.infrastructure import schema as schema_module
from app.infrastructure.repositories import auto_task_repo as auto_task_repo_module
from app.infrastructure.repositories.auto_task_repo import AutoTaskRepo


PAST = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


@asynccontextmanager
async def _repo(monkeypatch: pytest.MonkeyPatch):
    """Repo over a throwaway in-memory DB.

    The connection must be closed: aiosqlite runs each one on a non-daemon
    thread, so a leaked connection keeps the pytest process alive after the
    results are printed — which silently wedges the pre-push hook.
    """
    db = await aiosqlite.connect(":memory:")
    try:
        db.row_factory = aiosqlite.Row
        await schema_module.ensure_schema(db)

        async def fake_get_app_db():
            return db

        # Patch the module globals the repo's methods resolve against, matching
        # test_auto_task_repo.py.
        monkeypatch.setitem(
            auto_task_repo_module.AutoTaskRepo.create.__globals__,
            "get_app_db",
            fake_get_app_db,
        )
        yield AutoTaskRepo()
    finally:
        await db.close()


async def _due_task(repo: AutoTaskRepo) -> dict:
    task = await repo.create(
        "smith-id",
        {
            "title": "hourly sweep",
            "instruction": "do the thing",
            "working_dir": "/tmp/probe-workspace",
            "trigger_type": "interval",
            "trigger_config": "3600",
            "next_run_at": PAST,
        },
    )
    assert [row["id"] for row in await repo.list_due_tasks()] == [task["id"]]
    return task


@pytest.mark.asyncio
async def test_switching_to_manual_clears_the_pending_run(monkeypatch) -> None:
    async with _repo(monkeypatch) as repo:
        task = await _due_task(repo)

        updated = await repo.update(task["id"], {"trigger_type": "manual", "next_run_at": None})

        assert updated["trigger_type"] == "manual"
        assert updated["next_run_at"] in (None, ""), (
            f"a disabled task kept its pending run: {updated['next_run_at']!r}"
        )
        assert await repo.list_due_tasks() == [], "scheduler still sees the task as due"



@pytest.mark.asyncio
async def test_omitted_fields_are_left_untouched(monkeypatch) -> None:
    """Explicit None clears; an absent key must not."""
    async with _repo(monkeypatch) as repo:
        task = await _due_task(repo)

        updated = await repo.update(task["id"], {"title": "renamed"})

        assert updated["title"] == "renamed"
        assert updated["next_run_at"] == PAST, "an omitted field was cleared"



@pytest.mark.asyncio
async def test_required_columns_are_not_nulled(monkeypatch) -> None:
    """Clearing must stay scoped to genuinely nullable scheduling columns."""
    async with _repo(monkeypatch) as repo:
        task = await _due_task(repo)

        updated = await repo.update(task["id"], {"title": None, "instruction": None})

        assert updated["title"] == "hourly sweep"
        assert updated["instruction"] == "do the thing"



@pytest.mark.asyncio
async def test_lease_columns_can_be_released(monkeypatch) -> None:
    async with _repo(monkeypatch) as repo:
        task = await _due_task(repo)
        await repo.update(task["id"], {"lease_until": PAST, "lease_token": "tok"})

        updated = await repo.update(task["id"], {"lease_until": None, "lease_token": None})

        assert updated["lease_until"] in (None, "")
        assert updated["lease_token"] in (None, "")

