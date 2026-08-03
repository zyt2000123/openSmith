from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from ..database import get_app_db


# Scheduling columns that are meaningfully empty; everything else keeps its
# value when a caller passes None, so a partial update cannot null a NOT NULL
# column such as title or instruction.
_NULLABLE_FIELDS = frozenset({"last_run_at", "next_run_at", "lease_until", "lease_token"})


class AutoTaskRepo:

    # ── auto_tasks CRUD ──

    async def create(self, agent_id: str, data: dict) -> dict:
        db = await get_app_db()
        tid = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO auto_tasks "
            "(id, agent_id, title, description, trigger_type, trigger_config, "
            "instruction, working_dir, enabled, status, next_run_at, run_count, "
            "retry_count, "
            "max_retries, lease_until, lease_token, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tid,
                agent_id,
                data["title"],
                data.get("description", ""),
                data.get("trigger_type", "manual"),
                data.get("trigger_config", ""),
                data["instruction"],
                data["working_dir"],
                int(data.get("enabled", True)),
                "idle",
                data.get("next_run_at"),
                0,
                int(data.get("retry_count", 0)),
                int(data.get("max_retries", 2)),
                data.get("lease_until"),
                data.get("lease_token"),
                now,
            ),
        )
        await db.commit()
        row = await self.get(tid)
        if row is None:
            raise RuntimeError(f"Inserted auto task {tid!r} but could not re-fetch it")
        return row

    async def get(self, task_id: str) -> dict | None:
        db = await get_app_db()
        rows = await db.execute_fetchall(
            "SELECT * FROM auto_tasks WHERE id=?", (task_id,)
        )
        if not rows:
            return None
        return self._row_to_dict(rows[0])

    async def list_by_agent(self, agent_id: str) -> list[dict]:
        db = await get_app_db()
        rows = await db.execute_fetchall(
            "SELECT * FROM auto_tasks WHERE agent_id=? ORDER BY created_at DESC",
            (agent_id,),
        )
        return [self._row_to_dict(r) for r in rows]

    async def update(self, task_id: str, updates: dict) -> dict | None:
        existing = await self.get(task_id)
        if existing is None:
            return None

        db = await get_app_db()
        set_parts: list[str] = []
        params: list = []

        for field in (
            "title", "description", "trigger_type", "trigger_config",
            "instruction", "working_dir", "status", "last_run_at", "next_run_at",
            "retry_count", "max_retries", "lease_until", "lease_token",
        ):
            if field not in updates:
                continue
            # A caller that omits a key means "leave it"; one that passes None
            # for a nullable scheduling column means "clear it".  Collapsing
            # those two left a task switched to `manual` still carrying its old
            # next_run_at, and list_due_tasks does not filter on trigger_type —
            # so the scheduler ran it once more after the user disabled it.
            if updates[field] is None and field not in _NULLABLE_FIELDS:
                continue
            set_parts.append(f"{field}=?")
            params.append(updates[field])

        if "enabled" in updates and updates["enabled"] is not None:
            set_parts.append("enabled=?")
            params.append(int(updates["enabled"]))

        if "run_count" in updates and updates["run_count"] is not None:
            set_parts.append("run_count=?")
            params.append(updates["run_count"])

        if not set_parts:
            return existing

        params.append(task_id)
        await db.execute(
            f"UPDATE auto_tasks SET {', '.join(set_parts)} WHERE id=?", params
        )
        await db.commit()
        return await self.get(task_id)

    async def delete(self, task_id: str) -> bool:
        db = await get_app_db()
        rows = await db.execute_fetchall(
            "SELECT id FROM auto_tasks WHERE id=?", (task_id,)
        )
        if not rows:
            return False
        await db.execute("DELETE FROM auto_tasks WHERE id=?", (task_id,))
        await db.commit()
        return True

    async def claim_running(self, task_id: str) -> str | None:
        """Claim a task with an expiring, owner-bound execution lease."""
        db = await get_app_db()
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        lease_until = (now + timedelta(minutes=15)).isoformat()
        lease_token = uuid.uuid4().hex
        cursor = await db.execute(
            "UPDATE auto_tasks SET status='running', lease_until=?, lease_token=? "
            "WHERE id=? AND (status != 'running' OR lease_until IS NULL "
            "OR lease_until <= ?)",
            (lease_until, lease_token, task_id, now_text),
        )
        await db.commit()
        return lease_token if cursor.rowcount == 1 else None

    async def renew_lease(self, task_id: str, lease_token: str) -> bool:
        """Extend a live lease only when this worker still owns it."""
        db = await get_app_db()
        lease_until = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        cursor = await db.execute(
            "UPDATE auto_tasks SET lease_until=? "
            "WHERE id=? AND status='running' AND lease_token=?",
            (lease_until, task_id, lease_token),
        )
        await db.commit()
        return cursor.rowcount == 1

    async def finish_task(
        self,
        task_id: str,
        status: str,
        next_run_at: str | None,
        lease_token: str,
        *,
        retry_count: int | None = None,
    ) -> bool:
        """Finish only the lease held by this worker and atomically update retries."""
        db = await get_app_db()
        now = datetime.now(timezone.utc).isoformat()
        cursor = await db.execute(
            "UPDATE auto_tasks SET status=?, last_run_at=?, next_run_at=?, retry_count=COALESCE(?, retry_count), "
            "lease_until=NULL, lease_token=NULL, run_count = run_count + 1 "
            "WHERE id=? AND status='running' AND lease_token=?",
            (status, now, next_run_at, retry_count, task_id, lease_token),
        )
        await db.commit()
        return cursor.rowcount == 1

    async def list_due_tasks(self) -> list[dict]:
        """Find enabled tasks whose next_run_at <= now."""
        db = await get_app_db()
        now = datetime.now(timezone.utc).isoformat()
        rows = await db.execute_fetchall(
            "SELECT * FROM auto_tasks "
            "WHERE enabled=1 AND (status != 'running' OR lease_until IS NULL "
            "OR lease_until <= ?) AND next_run_at IS NOT NULL AND next_run_at <= ?",
            (now, now),
        )
        return [self._row_to_dict(r) for r in rows]

    # ── auto_task_runs CRUD ──

    async def create_run(self, auto_task_id: str) -> dict:
        db = await get_app_db()
        rid = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO auto_task_runs (id, auto_task_id, status, output, started_at) "
            "VALUES (?,?,?,?,?)",
            (rid, auto_task_id, "running", "", now),
        )
        await db.commit()
        return {
            "id": rid,
            "auto_task_id": auto_task_id,
            "status": "running",
            "output": "",
            "started_at": now,
            "finished_at": None,
            "error": None,
        }

    async def finish_run(
        self,
        run_id: str,
        status: str,
        output: str,
        error: str | None = None,
        *,
        auto_task_id: str | None = None,
        lease_token: str | None = None,
        force: bool = False,
    ) -> dict | None:
        """Record a run outcome only while the caller still owns the task lease.

        Without the gate, a worker whose 15-minute lease expired mid-run (and so
        lost the task to a reclaim) could still write a stale run row while a
        second worker executes the same instruction.  ``force=True`` bypasses the
        lease gate for this worker's OWN run row (cancellation or completed-with-
        lost-lease), where the row must be finalized to avoid a phantom 'running'
        row.  Even a force write never downgrades a row that is no longer
        'running' (e.g. a completed run hit by a late cancellation).
        """
        db = await get_app_db()
        if auto_task_id is not None and not force:
            rows = await db.execute_fetchall(
                "SELECT status, lease_token FROM auto_tasks WHERE id=?",
                (auto_task_id,),
            )
            if (
                not rows
                or rows[0]["status"] != "running"
                or rows[0]["lease_token"] != lease_token
            ):
                return None
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "UPDATE auto_task_runs SET status=?, output=?, finished_at=?, error=? "
            "WHERE id=? AND status='running'",
            (status, output, now, error, run_id),
        )
        await db.commit()
        rows = await db.execute_fetchall(
            "SELECT * FROM auto_task_runs WHERE id=?", (run_id,)
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "id": r["id"],
            "auto_task_id": r["auto_task_id"],
            "status": r["status"],
            "output": r["output"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "error": r["error"],
        }

    async def list_runs(self, auto_task_id: str) -> list[dict]:
        db = await get_app_db()
        rows = await db.execute_fetchall(
            "SELECT * FROM auto_task_runs WHERE auto_task_id=? ORDER BY started_at DESC",
            (auto_task_id,),
        )
        return [
            {
                "id": r["id"],
                "auto_task_id": r["auto_task_id"],
                "status": r["status"],
                "output": r["output"],
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "error": r["error"],
            }
            for r in rows
        ]

    # ── helpers ──

    @staticmethod
    def _row_to_dict(row) -> dict:
        return {
            "id": row["id"],
            "agent_id": row["agent_id"],
            "title": row["title"],
            "description": row["description"],
            "trigger_type": row["trigger_type"],
            "trigger_config": row["trigger_config"],
            "instruction": row["instruction"],
            "working_dir": row["working_dir"],
            "enabled": bool(row["enabled"]),
            "status": row["status"],
            "last_run_at": row["last_run_at"],
            "next_run_at": row["next_run_at"],
            "run_count": row["run_count"],
            "retry_count": row["retry_count"],
            "max_retries": row["max_retries"],
            "lease_until": row["lease_until"],
            "lease_token": row["lease_token"],
            "created_at": row["created_at"],
        }
