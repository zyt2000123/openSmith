from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from engine.execution import EngineRequest, reply_with_runtime as engine_reply_with_runtime

from ..schemas.auto_task import AutoTaskCreate, AutoTaskUpdate, AutoTaskOut, AutoTaskRunOut
from ..infrastructure.repositories.auto_task_repo import AutoTaskRepo
from ..infrastructure.repositories.agent_profile_repo import AgentProfileRepo
from ..infrastructure.repositories.session_repo import SessionRepo
from .engine_runtime import build_engine_runtime, load_runtime_identity_catalog
from ..utils.cron import next_cron_time, next_interval_time

log = logging.getLogger(__name__)
_RETRY_BASE_DELAY_SECONDS = 60
_MAX_RETRY_DELAY_SECONDS = 900
_LEASE_RENEW_INTERVAL_SECONDS = 60
# Concurrent detached runs. Each one drives a full engine turn, so this is a cap
# on simultaneous LLM work, not on throughput: deferred tasks stay due.
_MAX_CONCURRENT_RUNS = 4
# Module-scoped, not an instance attribute: AutoTaskService is rebuilt per HTTP
# request and per scheduler tick, so an instance would drop its strong reference
# and let the event loop garbage-collect a run that is still going.
_BACKGROUND_RUNS: set[asyncio.Task] = set()


def _forget_background_run(job: asyncio.Task) -> None:
    """Release the finished job and retrieve its exception so asyncio stays quiet."""
    _BACKGROUND_RUNS.discard(job)
    if not job.cancelled() and job.exception() is not None:
        log.error("Detached auto task run failed", exc_info=job.exception())


async def cancel_background_runs() -> None:
    """Cancel detached runs at shutdown.

    A cancelled run leaves its task at status='running'; _reset_stuck_auto_tasks()
    in the schema migration resets exactly that on the next startup, so there is
    no extra bookkeeping to do here.
    """
    jobs = list(_BACKGROUND_RUNS)
    for job in jobs:
        job.cancel()
    await asyncio.gather(*jobs, return_exceptions=True)


class AutoTaskService:

    def __init__(
        self,
        auto_task_repo: AutoTaskRepo,
        agent_profile_repo: AgentProfileRepo,
        session_repo: SessionRepo,
    ) -> None:
        self.repo = auto_task_repo
        self.agent_profile_repo = agent_profile_repo
        self.session_repo = session_repo

    # ── CRUD ──

    async def create_auto_task(
        self, agent_id: str, body: AutoTaskCreate
    ) -> AutoTaskOut:
        profile = await self.agent_profile_repo.get(agent_id)
        if profile is None:
            raise HTTPException(404, "Agent profile not found")

        next_run = self._require_next_run(body.trigger_type, body.trigger_config)

        row = await self.repo.create(agent_id, {
            **body.model_dump(),
            "next_run_at": next_run,
        })
        return AutoTaskOut(**row)

    async def list_auto_tasks(self, agent_id: str) -> list[AutoTaskOut]:
        rows = await self.repo.list_by_agent(agent_id)
        return [AutoTaskOut(**r) for r in rows]

    async def get_auto_task(self, task_id: str) -> AutoTaskOut:
        row = await self.repo.get(task_id)
        if row is None:
            raise HTTPException(404, "Auto task not found")
        return AutoTaskOut(**row)

    async def update_auto_task(
        self, task_id: str, body: AutoTaskUpdate
    ) -> AutoTaskOut:
        existing = await self.repo.get(task_id)
        if existing is None:
            raise HTTPException(404, "Auto task not found")

        updates = body.model_dump(exclude_none=True)

        # Recalculate next_run_at if trigger changed
        new_type = updates.get("trigger_type", existing["trigger_type"])
        new_config = updates.get("trigger_config", existing["trigger_config"])
        if "trigger_type" in updates or "trigger_config" in updates:
            updates["next_run_at"] = self._require_next_run(new_type, new_config)

        row = await self.repo.update(task_id, updates)
        if row is None:
            # Deleted by a concurrent request between the existence check and the update.
            raise HTTPException(404, "Auto task not found")
        return AutoTaskOut(**row)

    async def delete_auto_task(self, task_id: str) -> None:
        deleted = await self.repo.delete(task_id)
        if not deleted:
            raise HTTPException(404, "Auto task not found")

    # ── Trigger / Run ──

    async def trigger_auto_task(self, task_id: str) -> AutoTaskRunOut:
        """Accept one run of an auto task; the run itself proceeds in the background."""
        task = await self.repo.get(task_id)
        if task is None:
            raise HTTPException(404, "Auto task not found")
        started = await self.start_auto_task(task)
        if started is None:
            raise HTTPException(409, "Auto task is already running")
        return started

    async def start_auto_task(self, task: dict) -> AutoTaskRunOut | None:
        """Claim the task, then detach execution. None means another worker owns it.

        The claim and the run row are written synchronously so the caller learns
        at once whether it won and gets a run id to poll, while the engine turn
        never holds an HTTP request or a scheduler tick open.  That turn renews a
        15-minute lease, so it is expected to outlive any request timeout.
        """
        claim = await self._claim(task)
        if claim is None:
            return None
        run, lease_token = claim
        job = asyncio.create_task(self._execute(task, run, lease_token))
        _BACKGROUND_RUNS.add(job)
        job.add_done_callback(_forget_background_run)
        return AutoTaskRunOut(**run)

    async def _claim(self, task: dict) -> tuple[dict, str] | None:
        lease_token = await self.repo.claim_running(task["id"])
        if lease_token is None:
            return None
        return await self.repo.create_run(task["id"]), lease_token

    async def _execute(self, task: dict, run: dict, lease_token: str) -> AutoTaskRunOut:
        """Execute: create a session, send the instruction to engine, save the run."""
        task_id = task["id"]
        agent_id = task["agent_id"]

        next_run = self._calc_next_run(task["trigger_type"], task["trigger_config"])
        lease_renewal = asyncio.create_task(
            self._renew_lease_until_finished(task_id, lease_token)
        )

        try:
            profile = await self.agent_profile_repo.get(agent_id)
            profile_name = profile["name"] if profile else "Agent"
            working_dir = task.get("working_dir")
            if not isinstance(working_dir, str) or not working_dir.strip():
                raise RuntimeError(
                    "auto task has no working directory; update the task before running it"
                )

            identity_id = load_runtime_identity_catalog().resolve(
                task["instruction"]
            ).identity_id
            session = await self.session_repo.create(
                agent_id,
                f"[自动] {task['title']}",
                identity_id,
            )

            await self.session_repo.add_message(
                session["id"], "user", task["instruction"]
            )

            runtime, services = build_engine_runtime(
                agent_id,
                profile_name,
                session_id=session["id"],
            )
            result = await engine_reply_with_runtime(
                EngineRequest(
                    message=task["instruction"],
                    identity_id=identity_id,
                    working_dir=working_dir,
                ),
                runtime,
                services,
            )
            reply_text = result.text

            await self.session_repo.add_message(
                session["id"], "assistant", reply_text
            )

            if not await self.repo.finish_task(
                task_id,
                "idle",
                next_run,
                lease_token,
                retry_count=0,
            ):
                raise RuntimeError("auto task lease was lost before completion")
            finished = await self.repo.finish_run(run["id"], "completed", reply_text)
            if finished is None:
                raise HTTPException(500, "Failed to record auto task run")
            return AutoTaskRunOut(**finished)

        except Exception as exc:
            log.exception("Auto task %s failed", task_id)
            is_scheduled = task.get("trigger_type") != "manual"
            retry_count = int(task.get("retry_count") or 0) + 1 if is_scheduled else 0
            max_retries = max(0, int(task.get("max_retries", 2) or 0))
            retry_at = next_run
            retry_status = "failed"
            if is_scheduled and retry_count <= max_retries:
                retry_status = "idle"
                retry_at = self._retry_next_run(retry_count)
            finished_task = await self.repo.finish_task(
                task_id,
                retry_status,
                retry_at,
                lease_token,
                retry_count=retry_count if retry_status == "idle" else 0,
            )
            if not finished_task:
                log.warning("Auto task %s lease was lost before failure handling", task_id)
            finished = await self.repo.finish_run(
                run["id"], "failed", "", error=str(exc)
            )
            if finished is None:
                raise HTTPException(500, "Failed to record auto task run") from exc
            return AutoTaskRunOut(**finished)
        finally:
            lease_renewal.cancel()
            try:
                await lease_renewal
            except asyncio.CancelledError:
                pass

    async def list_runs(self, task_id: str) -> list[AutoTaskRunOut]:
        rows = await self.repo.list_runs(task_id)
        return [AutoTaskRunOut(**r) for r in rows]

    # ── Scheduler entry point ──

    async def tick(self) -> int:
        """Called by the scheduler. Start all due tasks. Returns the count started.

        Starting is not running: awaiting each task here let one slow engine turn
        block every other due task, the memory maintenance that follows, and the
        next tick.  The old return value counted due rows rather than claims.
        """
        due = await self.repo.list_due_tasks()
        started = 0
        for index, task in enumerate(due):
            if len(_BACKGROUND_RUNS) >= _MAX_CONCURRENT_RUNS:
                # Deferred, not dropped: an unclaimed task stays due, so a later
                # tick picks it up once a slot frees.
                log.info(
                    "Scheduler deferred %d due task(s); %d already running",
                    len(due) - index,
                    len(_BACKGROUND_RUNS),
                )
                break
            try:
                if await self.start_auto_task(task) is not None:
                    started += 1
            except Exception:
                log.exception("Scheduler failed to start task %s", task["id"])
        return started

    async def _renew_lease_until_finished(self, task_id: str, lease_token: str) -> None:
        """Keep ownership alive while an LLM/tool run outlives the initial lease."""
        try:
            while True:
                await asyncio.sleep(_LEASE_RENEW_INTERVAL_SECONDS)
                if not await self.repo.renew_lease(task_id, lease_token):
                    log.warning("Auto task %s lease was lost while running", task_id)
                    return
        except asyncio.CancelledError:
            raise

    # ── helpers ──

    @staticmethod
    def _next_run(trigger_type: str, trigger_config: str) -> str | None:
        """Strict: propagates why an expression cannot produce a next run time."""
        if trigger_type == "manual":
            return None
        now = datetime.now(timezone.utc)
        if trigger_type == "cron":
            return next_cron_time(trigger_config, after=now).isoformat()
        if trigger_type == "interval":
            return next_interval_time(int(trigger_config), after=now).isoformat()
        return None

    @classmethod
    def _calc_next_run(cls, trigger_type: str, trigger_config: str) -> str | None:
        """Tolerant: _execute reschedules from a stored row it cannot reject."""
        try:
            return cls._next_run(trigger_type, trigger_config)
        except (ValueError, TypeError, OverflowError):
            return None

    @classmethod
    def _require_next_run(cls, trigger_type: str, trigger_config: str) -> str | None:
        """Reject a trigger the scheduler could never fire.

        list_due_tasks skips a NULL next_run_at, so storing the None that
        _calc_next_run returns for an unparseable or unsatisfiable expression
        creates a task that silently never runs. Only the write paths validate:
        _execute stays tolerant so one bad legacy row cannot break its own run
        bookkeeping.
        """
        try:
            return cls._next_run(trigger_type, trigger_config)
        except (ValueError, TypeError, OverflowError) as exc:
            raise HTTPException(422, f"invalid {trigger_type} trigger_config: {exc}") from exc

    @staticmethod
    def _retry_next_run(attempt: int) -> str:
        delay = min(
            _RETRY_BASE_DELAY_SECONDS * (2 ** max(0, attempt - 1)),
            _MAX_RETRY_DELAY_SECONDS,
        )
        return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
