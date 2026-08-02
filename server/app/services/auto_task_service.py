from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from fastapi import HTTPException

from engine.execution import EngineRequest, reply_with_runtime as engine_reply_with_runtime
from engine.observability.trace_store import _redact_secrets_in_text

from ..schemas.auto_task import (
    MAX_RETRIES_CAP,
    MIN_INTERVAL_SECONDS,
    AutoTaskCreate,
    AutoTaskUpdate,
    AutoTaskOut,
    AutoTaskRunOut,
)
from ..infrastructure.repositories.auto_task_repo import AutoTaskRepo
from ..infrastructure.repositories.agent_profile_repo import AgentProfileRepo
from ..infrastructure.repositories.session_repo import SessionRepo
from .engine_runtime import build_engine_runtime, load_runtime_identity_catalog
from ..utils.cron import next_cron_time, next_interval_time

log = logging.getLogger(__name__)
_RETRY_BASE_DELAY_SECONDS = 60
_MAX_RETRY_DELAY_SECONDS = 900
_LEASE_RENEW_INTERVAL_SECONDS = 60
# Safety net for a whole auto-task run.  Individual LLM requests and tool calls
# have their own timeouts; this caps a pathological multi-turn loop so a hung
# run cannot hold a _MAX_CONCURRENT_RUNS slot (and its lease) forever.
_TASK_EXECUTION_TIMEOUT_SECONDS = 1800
# Concurrent detached runs. Each one drives a full engine turn, so this is a cap
# on simultaneous LLM work, not on throughput: deferred tasks stay due.
_MAX_CONCURRENT_RUNS = 4
# Module-scoped, not an instance attribute: AutoTaskService is rebuilt per HTTP
# request and per scheduler tick, so an instance would drop its strong reference
# and let the event loop garbage-collect a run that is still going.
_BACKGROUND_RUNS: set[asyncio.Task] = set()

# Reserved-but-not-yet-started slots.  start_auto_task checks len(_BACKGROUND_RUNS)
# against the cap and then awaits a DB claim before adding the task, so two
# concurrent triggers could both pass the check.  Reserving the slot BEFORE the
# first await closes that TOCTOU window.
_RESERVED_SLOTS = 0


def _reserve_run_slot() -> bool:
    global _RESERVED_SLOTS
    if _RESERVED_SLOTS + len(_BACKGROUND_RUNS) >= _MAX_CONCURRENT_RUNS:
        return False
    _RESERVED_SLOTS += 1
    return True


def _release_run_slot() -> None:
    global _RESERVED_SLOTS
    _RESERVED_SLOTS = max(0, _RESERVED_SLOTS - 1)


def _forget_background_run(job: asyncio.Task) -> None:
    """Release the finished job and retrieve its exception so asyncio stays quiet."""
    _BACKGROUND_RUNS.discard(job)
    if not job.cancelled() and job.exception() is not None:
        log.error("Detached auto task run failed", exc_info=job.exception())


def _redact_error_text(exc: BaseException) -> str:
    """Persist a failure reason without embedding credentials from error text.

    httpx/engine errors can echo the full request URL or provider response;
    strip the credential shapes the trace store redacts before the text is
    stored and later served to any authenticated caller.
    """
    return _redact_secrets_in_text(str(exc))[:500]


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

    async def get_auto_task(self, agent_id: str, task_id: str) -> AutoTaskOut:
        row = await self.repo.get(task_id)
        if row is None or row["agent_id"] != agent_id:
            raise HTTPException(404, "Auto task not found")
        return AutoTaskOut(**row)

    async def update_auto_task(
        self, agent_id: str, task_id: str, body: AutoTaskUpdate
    ) -> AutoTaskOut:
        existing = await self.repo.get(task_id)
        if existing is None or existing["agent_id"] != agent_id:
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

    async def delete_auto_task(self, agent_id: str, task_id: str) -> None:
        row = await self.repo.get(task_id)
        if row is None or row["agent_id"] != agent_id:
            raise HTTPException(404, "Auto task not found")
        deleted = await self.repo.delete(task_id)
        if not deleted:
            raise HTTPException(404, "Auto task not found")

    # ── Trigger / Run ──

    async def trigger_auto_task(self, agent_id: str, task_id: str) -> AutoTaskRunOut:
        """Accept one run of an auto task; the run itself proceeds in the background."""
        task = await self.repo.get(task_id)
        if task is None or task["agent_id"] != agent_id:
            raise HTTPException(404, "Auto task not found")
        if not _reserve_run_slot():
            raise HTTPException(
                429, "Too many auto tasks are already running; try again later"
            )
        try:
            started = await self._start_claimed(task)
        finally:
            _release_run_slot()
        if started is None:
            raise HTTPException(409, "Auto task is already running")
        return started

    async def start_auto_task(self, task: dict) -> AutoTaskRunOut | None:
        """Claim the task, then detach execution. None means another worker owns it.

        The claim and the run row are written synchronously so the caller learns
        at once whether it won and gets a run id to poll, while the engine turn
        never holds an HTTP request or a scheduler tick open.  That turn renews a
        15-minute lease, so it is expected to outlive any request timeout.

        The concurrency cap is enforced here (not just in tick) so a manual
        trigger cannot start more detached runs than a scheduled tick would.
        The slot is reserved before the first await so concurrent triggers
        cannot both pass the cap check (TOCTOU).
        """
        if not _reserve_run_slot():
            return None
        try:
            return await self._start_claimed(task)
        finally:
            _release_run_slot()

    async def _start_claimed(self, task: dict) -> AutoTaskRunOut | None:
        """Claim and detach; the caller already holds a reserved run slot."""
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
        trigger_type = task["trigger_type"]
        trigger_config = task["trigger_config"]
        run_finalized: dict | None = None

        # If the lease is lost mid-run (renewal failed: another worker reclaimed
        # the task, or the process is shutting down) the engine turn MUST stop:
        # continuing would duplicate side effects and double LLM billing while a
        # second worker executes the same instruction.
        current_task = asyncio.current_task()
        lease_renewal = asyncio.create_task(
            self._renew_lease_until_finished(
                task_id, lease_token, on_lost=current_task.cancel
            )
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
            try:
                result = await asyncio.wait_for(
                    engine_reply_with_runtime(
                        EngineRequest(
                            message=task["instruction"],
                            identity_id=identity_id,
                            working_dir=working_dir,
                        ),
                        runtime,
                        services,
                    ),
                    timeout=_TASK_EXECUTION_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                raise RuntimeError(
                    f"auto task timed out after {_TASK_EXECUTION_TIMEOUT_SECONDS}s"
                ) from None
            reply_text = result.text

            await self.session_repo.add_message(
                session["id"], "assistant", reply_text
            )

            # Compute the next slot from completion time, not start time: an
            # interval/cron task whose execution outlives its schedule must not
            # be immediately due again.
            next_run = self._calc_next_run(trigger_type, trigger_config)
            finished = await self.repo.finish_run(
                run["id"],
                "completed",
                reply_text,
                auto_task_id=task_id,
                lease_token=lease_token,
            )
            if finished is None:
                # Lease was lost between renewal and finish (another worker
                # reclaimed the task). The side effects already happened, so
                # surface the run as completed without touching the
                # lease-guarded run row.
                log.warning(
                    "Auto task %s run %s not recorded: lease no longer held",
                    task_id,
                    run["id"],
                )
                finished = {
                    **run,
                    "status": "completed",
                    "output": reply_text,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
            run_finalized = finished
            try:
                lease_released = await self.repo.finish_task(
                    task_id,
                    "idle",
                    next_run,
                    lease_token,
                    retry_count=0,
                )
            except Exception:
                # Bookkeeping failure after the run was recorded as completed.
                # Never downgrade the run to failed or reschedule a retry, which
                # would re-apply the engine's side effects; release the lease
                # best-effort so the task is not stuck 'running' until expiry.
                log.exception("failed to finalize auto task %s after completion", task_id)
                lease_released = False
                try:
                    await self.repo.finish_task(
                        task_id, "idle", next_run, lease_token, retry_count=0
                    )
                except Exception:
                    log.warning(
                        "auto task %s lease was not released; it will expire and be reclaimed",
                        task_id,
                        exc_info=True,
                    )
            if not lease_released:
                # Either another worker reclaimed the task (inherent to the lease
                # design) or a retry above failed; either way this run is final.
                log.warning("Auto task %s lease was lost after completion", task_id)
            return AutoTaskRunOut(**run_finalized)

        except asyncio.CancelledError:
            # Lease was lost mid-run (or shutdown cancelled the run). Another
            # worker may own the task, so finish_task must NOT run (we no longer
            # hold the token).  If the run already completed (side effects fully
            # applied, run_finalized set) never downgrade it to failed; a late
            # cancellation landing after completion must not erase the outcome.
            # Otherwise mark the run failed even though the lease is gone — the
            # run row is this worker's own, and leaving it 'running' would make
            # it a phantom row forever (the lease reset only reclaims expired
            # tasks, not live-lease ones).
            if run_finalized is None:
                try:
                    await self.repo.finish_run(
                        run["id"],
                        "failed",
                        "",
                        error="auto task lease was lost",
                        auto_task_id=task_id,
                        lease_token=lease_token,
                        force=True,
                    )
                except Exception:
                    log.warning(
                        "failed to mark auto task run %s failed after lease loss",
                        run["id"],
                        exc_info=True,
                    )
            raise

        except Exception as exc:
            if run_finalized is not None:
                # Defensive: the success path returns before any later statement,
                # but never turn a completed run into a failed one.
                log.exception("unexpected error after auto task %s completed", task_id)
                return AutoTaskRunOut(**run_finalized)
            log.exception("Auto task %s failed", task_id)
            is_scheduled = trigger_type != "manual"
            retry_count = int(task.get("retry_count") or 0) + 1 if is_scheduled else 0
            max_retries = min(MAX_RETRIES_CAP, max(0, int(task.get("max_retries", 2) or 0)))
            retry_status = "failed"
            if is_scheduled and retry_count <= max_retries:
                retry_status = "idle"
                retry_at = self._retry_next_run(retry_count)
            else:
                # Non-retryable failure: schedule the next slot from now so a
                # task whose execution outlived its interval does not immediately
                # loop through failure again.
                retry_at = self._calc_next_run(trigger_type, trigger_config)
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
                run["id"],
                "failed",
                "",
                error=_redact_error_text(exc),
                auto_task_id=task_id,
                lease_token=lease_token,
            )
            if finished is None:
                # Lease lost before failure handling: do not blow up the detached
                # task over bookkeeping, and never retry a superseded run.
                log.warning(
                    "Auto task %s run %s failure not recorded: lease no longer held",
                    task_id,
                    run["id"],
                )
                finished = {
                    **run,
                    "status": "failed",
                    "error": _redact_error_text(exc),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
            return AutoTaskRunOut(**finished)
        finally:
            lease_renewal.cancel()
            try:
                await lease_renewal
            except asyncio.CancelledError:
                pass

    async def list_runs(self, agent_id: str, task_id: str) -> list[AutoTaskRunOut]:
        row = await self.repo.get(task_id)
        if row is None or row["agent_id"] != agent_id:
            raise HTTPException(404, "Auto task not found")
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

    async def _renew_lease_until_finished(
        self, task_id: str, lease_token: str, on_lost: Callable[[], None]
    ) -> None:
        """Keep ownership alive while an LLM/tool run outlives the initial lease.

        Losing the lease mid-run means another worker owns the task now; call
        ``on_lost`` so the owner cancels the engine turn instead of continuing.
        A renewal error is treated the same way: the lease will expire and a
        second worker may reclaim the task, so the turn must stop.
        """
        try:
            while True:
                await asyncio.sleep(_LEASE_RENEW_INTERVAL_SECONDS)
                if not await self.repo.renew_lease(task_id, lease_token):
                    log.warning("Auto task %s lease was lost while running", task_id)
                    on_lost()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Auto task %s lease renewal failed; treating the lease as lost",
                task_id,
            )
            on_lost()

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
            seconds = int(trigger_config)
            if seconds < MIN_INTERVAL_SECONDS:
                raise ValueError(
                    f"interval must be at least {MIN_INTERVAL_SECONDS}s"
                )
            return next_interval_time(seconds, after=now).isoformat()
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
