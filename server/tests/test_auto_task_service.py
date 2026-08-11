from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.schemas.auto_task import AutoTaskCreate, AutoTaskUpdate  # noqa: E402
from app.services import auto_task_service as auto_task_service_module  # noqa: E402
from app.services.auto_task_service import AutoTaskService  # noqa: E402


def _task(task_id: str = "task-1", **overrides) -> dict:
    return {
        "id": task_id,
        "agent_id": "smith-id",
        "title": "日报",
        "instruction": "生成日报",
        "working_dir": "/tmp/project",
        "trigger_type": "manual",
        "trigger_config": "",
        "run_count": 0,
        **overrides,
    }


class FakeAutoTaskRepo:
    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.finished: list[dict] = []

    async def get(self, task_id: str) -> dict:
        return _task(task_id)

    async def claim_running(self, task_id: str) -> str | None:
        return "lease-token"

    async def finish_task(
        self,
        task_id: str,
        status: str,
        next_run_at: str | None,
        lease_token: str,
        *,
        retry_count: int | None = None,
    ) -> bool:
        self.updates.append({
            "task_id": task_id,
            "status": status,
            "next_run_at": next_run_at,
            "lease_token": lease_token,
            "retry_count": retry_count,
        })
        return True

    async def renew_lease(self, task_id: str, lease_token: str) -> bool:
        self.updates.append({"renewed_task_id": task_id, "lease_token": lease_token})
        return True

    async def update(self, task_id: str, updates: dict):
        self.updates.append(dict(updates))

    async def create_run(self, task_id: str) -> dict:
        return {
            "id": "run-1",
            "auto_task_id": task_id,
            "status": "running",
            "output": "",
            "started_at": "2026-07-11T00:00:00Z",
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
    ) -> dict:
        row = {
            "id": run_id,
            "auto_task_id": "task-1",
            "status": status,
            "output": output,
            "started_at": "2026-07-11T00:00:00Z",
            "finished_at": "2026-07-11T00:01:00Z",
            "error": error,
        }
        self.finished.append(row)
        return row


class FakeProfileRepo:
    async def get(self, agent_id: str) -> dict:
        return {"id": agent_id, "name": "Smith"}


class FakeSessionRepo:
    def __init__(self) -> None:
        self.created: list[tuple[str, str, str | None]] = []
        self.messages: list[tuple[str, str, str]] = []

    async def create(self, agent_id: str, title: str, identity_id: str | None = None) -> dict:
        self.created.append((agent_id, title, identity_id))
        return {"id": "session-1", "agent_id": agent_id, "identity_id": identity_id}

    async def add_message(self, session_id: str, role: str, content: str) -> dict:
        self.messages.append((session_id, role, content))
        return {"id": f"{role}-1", "session_id": session_id, "role": role, "content": content}


def _stub_engine(monkeypatch: pytest.MonkeyPatch, reply) -> None:
    class Catalog:
        def resolve(self, message: str):
            return SimpleNamespace(identity_id="smith")

    monkeypatch.setattr(auto_task_service_module, "load_runtime_identity_catalog", lambda: Catalog())
    monkeypatch.setattr(
        auto_task_service_module, "build_engine_runtime", lambda *args, **kwargs: (object(), object())
    )
    monkeypatch.setattr(auto_task_service_module, "engine_reply_with_runtime", reply)


async def _drain_background_runs() -> None:
    await asyncio.gather(*list(auto_task_service_module._BACKGROUND_RUNS), return_exceptions=True)
    await asyncio.sleep(0)  # let the done callbacks release the registry
    assert auto_task_service_module._BACKGROUND_RUNS == set()


async def _run_to_completion(service: AutoTaskService, task: dict) -> dict:
    """Drive one task through the production path and return the persisted run row.

    start_auto_task detaches execution, so an outcome has to be read from what the
    repository recorded — there is no return value left to inspect.
    """
    assert await service.start_auto_task(task) is not None
    await _drain_background_runs()
    return service.repo.finished[-1]


@pytest.mark.asyncio
async def test_trigger_returns_while_the_engine_turn_is_still_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manual trigger must not hold the HTTP request open for the whole run.

    The engine turn renews a 15-minute lease, so awaiting it inside the handler
    meant the client always timed out before the reply landed.
    """
    release = asyncio.Event()
    entered = asyncio.Event()

    async def blocking_reply(request, runtime, services):
        entered.set()
        await release.wait()
        return SimpleNamespace(text="日报已生成")

    _stub_engine(monkeypatch, blocking_reply)
    task_repo = FakeAutoTaskRepo()
    service = AutoTaskService(task_repo, FakeProfileRepo(), FakeSessionRepo())

    # release is never set here: this only returns because execution is detached.
    started = await asyncio.wait_for(service.trigger_auto_task("smith-id", "task-1"), timeout=1)
    await asyncio.wait_for(entered.wait(), timeout=1)

    assert started.status == "running"
    assert started.finished_at is None

    release.set()
    await _drain_background_runs()
    assert task_repo.updates[-1]["status"] == "idle"


@pytest.mark.asyncio
async def test_scheduler_tick_starts_due_tasks_without_waiting_for_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One slow engine turn must not block the other due tasks or the next tick.

    tick() used to await each run in turn, so a single long task froze every
    other due task, the memory maintenance that follows, and the whole loop.
    """
    release = asyncio.Event()
    both_running = asyncio.Event()
    in_flight = 0

    async def blocking_reply(request, runtime, services):
        nonlocal in_flight
        in_flight += 1
        if in_flight == 2:
            both_running.set()
        await release.wait()
        return SimpleNamespace(text="done")

    class Repo(FakeAutoTaskRepo):
        async def list_due_tasks(self) -> list[dict]:
            return [_task("task-1", trigger_type="cron"), _task("task-2", trigger_type="cron")]

    _stub_engine(monkeypatch, blocking_reply)
    service = AutoTaskService(Repo(), FakeProfileRepo(), FakeSessionRepo())

    assert await asyncio.wait_for(service.tick(), timeout=1) == 2
    # Times out if the tick serialized the two runs instead of detaching them.
    await asyncio.wait_for(both_running.wait(), timeout=1)

    # The concurrency cap defers rather than drops: unclaimed tasks stay due.
    monkeypatch.setattr(auto_task_service_module, "_MAX_CONCURRENT_RUNS", 2)
    assert await service.tick() == 0

    release.set()
    await _drain_background_runs()


@pytest.mark.asyncio
async def test_auto_task_writes_reject_a_trigger_the_scheduler_can_never_fire() -> None:
    """An unfireable trigger must fail the write, not become a silent no-op.

    _calc_next_run returns None for both "manual" and "unparseable", and
    list_due_tasks skips a NULL next_run_at — so storing None for a cron created
    a task that reported 201/200 and then never ran.
    """
    class Repo:
        def __init__(self) -> None:
            self.wrote = False

        async def get(self, task_id: str) -> dict:
            return {
                "id": task_id,
                "agent_id": "smith-id",
                "trigger_type": "cron",
                "trigger_config": "*/5 * * * *",
            }

        async def create(self, agent_id: str, data: dict) -> dict:
            self.wrote = True
            raise AssertionError("must not reach the repository")

        async def update(self, task_id: str, updates: dict) -> dict:
            self.wrote = True
            raise AssertionError("must not reach the repository")

    for bad in ("*/0 * * * *", "99 * * * *", "0 0 30 2 *", "not a cron"):
        repo = Repo()
        service = AutoTaskService(repo, FakeProfileRepo(), FakeSessionRepo())
        with pytest.raises(HTTPException) as created:
            await service.create_auto_task(
                "smith-id",
                AutoTaskCreate(
                    title="t",
                    instruction="i",
                    working_dir="/tmp/project",
                    trigger_type="cron",
                    trigger_config=bad,
                ),
            )
        # trigger_type omitted: the effective type comes from the stored row, so
        # only the service can decide whether this patch is schedulable.
        with pytest.raises(HTTPException) as updated:
            await service.update_auto_task(
                "smith-id", "task-1", AutoTaskUpdate(trigger_config=bad)
            )
        assert (created.value.status_code, updated.value.status_code) == (422, 422)
        assert repo.wrote is False

    assert AutoTaskService._require_next_run("cron", "*/5 * * * *") is not None
    assert AutoTaskService._require_next_run("cron", "0 9 * * 1-5") is not None
    assert AutoTaskService._require_next_run("manual", "") is None


@pytest.mark.asyncio
async def test_auto_task_mutations_reject_a_task_owned_by_another_agent() -> None:
    """task_id-scoped mutations must not cross the owning-agent boundary."""
    class ForeignRepo:
        async def get(self, task_id: str) -> dict:
            return {
                "id": task_id,
                "agent_id": "some-other-agent",
                "trigger_type": "interval",
                "trigger_config": "60",
            }

        async def delete(self, task_id: str) -> bool:
            raise AssertionError("must not delete another agent's task")

    service = AutoTaskService(ForeignRepo(), FakeProfileRepo(), FakeSessionRepo())

    with pytest.raises(HTTPException) as exc:
        await service.update_auto_task(
            "smith-id", "task-1", AutoTaskUpdate(trigger_config="120")
        )
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        await service.trigger_auto_task("smith-id", "task-1")
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        await service.delete_auto_task("smith-id", "task-1")
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        await service.list_runs("smith-id", "task-1")
    assert exc.value.status_code == 404


def test_auto_task_create_requires_a_nonempty_working_directory() -> None:
    with pytest.raises(ValueError, match="working_dir"):
        AutoTaskCreate(title="t", instruction="i", working_dir="   ")


@pytest.mark.asyncio
async def test_auto_task_pins_its_generated_session_to_the_resolved_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Catalog:
        def resolve(self, message: str):
            assert message == "审查这份合同"
            return SimpleNamespace(identity_id="legal")

    def fake_build_runtime(agent_id: str, name: str, *, session_id: str | None = None):
        captured["runtime"] = (agent_id, name, session_id)
        return object(), object()

    async def fake_reply(request, runtime, services):
        captured["request"] = request
        return SimpleNamespace(text="合同审查完成")

    monkeypatch.setattr(auto_task_service_module, "load_runtime_identity_catalog", lambda: Catalog())
    monkeypatch.setattr(auto_task_service_module, "build_engine_runtime", fake_build_runtime)
    monkeypatch.setattr(auto_task_service_module, "engine_reply_with_runtime", fake_reply)

    task_repo = FakeAutoTaskRepo()
    session_repo = FakeSessionRepo()
    service = AutoTaskService(task_repo, FakeProfileRepo(), session_repo)
    task = {
        "id": "task-1",
        "agent_id": "smith-id",
        "title": "合同检查",
        "instruction": "审查这份合同",
        "working_dir": "/workspace/contracts",
        "trigger_type": "manual",
        "trigger_config": "",
        "run_count": 0,
    }

    finished = await _run_to_completion(service, task)

    assert finished["status"] == "completed"
    assert session_repo.created == [("smith-id", "[自动] 合同检查", "legal")]
    assert captured["request"].identity_id == "legal"
    assert captured["request"].working_dir == "/workspace/contracts"


@pytest.mark.asyncio
async def test_failed_scheduled_auto_task_is_requeued_with_retry_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Catalog:
        def resolve(self, message: str):
            return SimpleNamespace(identity_id="smith")

    async def fail_reply(request, runtime, services):
        raise RuntimeError("temporary provider outage")

    monkeypatch.setattr(auto_task_service_module, "load_runtime_identity_catalog", lambda: Catalog())
    monkeypatch.setattr(auto_task_service_module, "build_engine_runtime", lambda *args, **kwargs: (object(), object()))
    monkeypatch.setattr(auto_task_service_module, "engine_reply_with_runtime", fail_reply)

    task_repo = FakeAutoTaskRepo()
    service = AutoTaskService(task_repo, FakeProfileRepo(), FakeSessionRepo())
    task = {
        "id": "task-1",
        "agent_id": "smith-id",
        "title": "定时检查",
        "instruction": "检查服务",
        "working_dir": "/tmp/project",
        "trigger_type": "interval",
        "trigger_config": "3600",
        "run_count": 0,
        "retry_count": 0,
        "max_retries": 2,
    }

    finished = await _run_to_completion(service, task)

    assert finished["status"] == "failed"
    assert any(update.get("retry_count") == 1 for update in task_repo.updates)
    retry_update = next(update for update in task_repo.updates if "retry_count" in update)
    assert retry_update["retry_count"] == 1
    task_update = next(update for update in task_repo.updates if update.get("task_id") == "task-1")
    assert task_update["status"] == "idle"
    assert task_update["next_run_at"] is not None


@pytest.mark.asyncio
async def test_failed_run_is_recorded_before_the_lease_is_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing run must finalize its own run row, never leave it phantom-'running'.

    Production's finish_run is lease-gated: with auto_task_id and force=False it
    writes only while the task is still 'running' with this token.  The failure
    path used to release the lease (finish_task) *before* recording the failure,
    so the gated finish_run found the lease gone and skipped the write — the run
    row stayed 'running' with error=NULL forever.  This repo models that gate.
    """
    class LeaseGatedRepo(FakeAutoTaskRepo):
        def __init__(self) -> None:
            super().__init__()
            self.lease_held = True
            self.forced: list[str] = []

        async def finish_task(self, task_id, status, next_run_at, lease_token, *, retry_count=None) -> bool:
            self.lease_held = False  # releasing the lease nulls the token
            return await super().finish_task(
                task_id, status, next_run_at, lease_token, retry_count=retry_count
            )

        async def finish_run(
            self, run_id, status, output, error=None, *,
            auto_task_id=None, lease_token=None, force=False,
        ) -> dict | None:
            if auto_task_id is not None and not force and not self.lease_held:
                return None  # the production gate: lease no longer held
            if force:
                self.forced.append(run_id)
            return await super().finish_run(
                run_id, status, output, error, auto_task_id=auto_task_id, lease_token=lease_token
            )

    async def fail_reply(request, runtime, services):
        raise RuntimeError("provider outage with a secret-free message")

    monkeypatch.setattr(
        auto_task_service_module, "load_runtime_identity_catalog",
        lambda: SimpleNamespace(resolve=lambda message: SimpleNamespace(identity_id="smith")),
    )
    monkeypatch.setattr(
        auto_task_service_module, "build_engine_runtime",
        lambda *args, **kwargs: (object(), object()),
    )
    monkeypatch.setattr(auto_task_service_module, "engine_reply_with_runtime", fail_reply)

    task_repo = LeaseGatedRepo()
    service = AutoTaskService(task_repo, FakeProfileRepo(), FakeSessionRepo())
    task = _task(trigger_type="interval", trigger_config="3600", retry_count=0, max_retries=2)

    finished = await _run_to_completion(service, task)

    # The run row was finalized as failed with the error text, not left running.
    assert finished["status"] == "failed"
    assert finished["error"] and "provider outage" in finished["error"]
    # The task was still rescheduled for retry afterwards.
    task_update = next(u for u in task_repo.updates if u.get("task_id") == "task-1")
    assert task_update["status"] == "idle"


@pytest.mark.asyncio
async def test_exhausted_retry_chain_resets_before_next_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_reply(request, runtime, services):
        raise RuntimeError("persistent provider outage")

    monkeypatch.setattr(
        auto_task_service_module,
        "load_runtime_identity_catalog",
        lambda: SimpleNamespace(resolve=lambda message: SimpleNamespace(identity_id="smith")),
    )
    monkeypatch.setattr(
        auto_task_service_module,
        "build_engine_runtime",
        lambda *args, **kwargs: (object(), object()),
    )
    monkeypatch.setattr(auto_task_service_module, "engine_reply_with_runtime", fail_reply)

    task_repo = FakeAutoTaskRepo()
    service = AutoTaskService(task_repo, FakeProfileRepo(), FakeSessionRepo())
    task = {
        "id": "task-1",
        "agent_id": "smith-id",
        "title": "定时检查",
        "instruction": "检查服务",
        "working_dir": "/tmp/project",
        "trigger_type": "interval",
        "trigger_config": "3600",
        "retry_count": 2,
        "max_retries": 2,
    }

    finished = await _run_to_completion(service, task)

    assert finished["status"] == "failed"
    assert any(
        update.get("task_id") == "task-1" and update.get("retry_count") == 0
        for update in task_repo.updates
    )


@pytest.mark.asyncio
async def test_auto_task_renews_its_lease_while_the_engine_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow_reply(request, runtime, services):
        await asyncio.sleep(0.01)
        return SimpleNamespace(text="finished")

    monkeypatch.setattr(
        auto_task_service_module,
        "load_runtime_identity_catalog",
        lambda: SimpleNamespace(resolve=lambda message: SimpleNamespace(identity_id="smith")),
    )
    monkeypatch.setattr(
        auto_task_service_module,
        "build_engine_runtime",
        lambda *args, **kwargs: (object(), object()),
    )
    monkeypatch.setattr(auto_task_service_module, "engine_reply_with_runtime", slow_reply)
    monkeypatch.setattr(auto_task_service_module, "_LEASE_RENEW_INTERVAL_SECONDS", 0.001)

    task_repo = FakeAutoTaskRepo()
    service = AutoTaskService(task_repo, FakeProfileRepo(), FakeSessionRepo())
    finished = await _run_to_completion(service, _task(title="slow check", instruction="check"))

    assert finished["status"] == "completed"
    assert any(update.get("renewed_task_id") == "task-1" for update in task_repo.updates)


@pytest.mark.asyncio
async def test_completed_run_is_recorded_completed_when_the_lease_is_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If another worker reclaimed the task before completion, the run here still
    completed: record it as completed and do not mark the task failed/rescheduled."""
    class LostLeaseRepo(FakeAutoTaskRepo):
        async def finish_task(
            self,
            task_id: str,
            status: str,
            next_run_at: str | None,
            lease_token: str,
            *,
            retry_count: int | None = None,
        ) -> bool:
            return False

    async def ok_reply(request, runtime, services):
        return SimpleNamespace(text="done")

    monkeypatch.setattr(
        auto_task_service_module,
        "load_runtime_identity_catalog",
        lambda: SimpleNamespace(resolve=lambda message: SimpleNamespace(identity_id="smith")),
    )
    monkeypatch.setattr(
        auto_task_service_module,
        "build_engine_runtime",
        lambda *args, **kwargs: (object(), object()),
    )
    monkeypatch.setattr(auto_task_service_module, "engine_reply_with_runtime", ok_reply)

    task_repo = LostLeaseRepo()
    service = AutoTaskService(task_repo, FakeProfileRepo(), FakeSessionRepo())
    finished = await _run_to_completion(service, _task())

    # The engine work happened and the assistant reply was persisted; the run
    # record must say completed, never failed, and no failure reschedule may occur.
    assert finished["status"] == "completed"


@pytest.mark.asyncio
async def test_completed_run_survives_a_finish_task_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If final bookkeeping (finish_task) raises after the run was recorded as
    completed, the run must stay completed: overwriting it with failed or
    rescheduling a retry would re-apply the engine's side effects."""
    class ExplodingFinalizeRepo(FakeAutoTaskRepo):
        def __init__(self) -> None:
            super().__init__()
            self.finish_task_attempts = 0

        async def finish_task(
            self,
            task_id: str,
            status: str,
            next_run_at: str | None,
            lease_token: str,
            *,
            retry_count: int | None = None,
        ) -> bool:
            self.finish_task_attempts += 1
            raise RuntimeError("database is locked")

    async def ok_reply(request, runtime, services):
        return SimpleNamespace(text="done")

    monkeypatch.setattr(
        auto_task_service_module,
        "load_runtime_identity_catalog",
        lambda: SimpleNamespace(resolve=lambda message: SimpleNamespace(identity_id="smith")),
    )
    monkeypatch.setattr(
        auto_task_service_module,
        "build_engine_runtime",
        lambda *args, **kwargs: (object(), object()),
    )
    monkeypatch.setattr(auto_task_service_module, "engine_reply_with_runtime", ok_reply)

    task_repo = ExplodingFinalizeRepo()
    service = AutoTaskService(task_repo, FakeProfileRepo(), FakeSessionRepo())
    finished = await _run_to_completion(service, _task())

    assert finished["status"] == "completed"
    # The lease release is retried once best-effort, but the run is never failed.
    assert task_repo.finish_task_attempts == 2
    assert task_repo.finished[-1]["status"] == "completed"
    assert not any(update.get("retry_count") == 1 for update in task_repo.updates)


@pytest.mark.asyncio
async def test_success_schedules_next_run_from_completion_not_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """next_run must be derived from when the run finished. A task whose execution
    outlives its interval would otherwise be immediately due again."""
    engine_state = {"finished": False}

    async def slow_reply(request, runtime, services):
        await asyncio.sleep(0.005)
        engine_state["finished"] = True
        return SimpleNamespace(text="done")

    def fake_calc_next_run(trigger_type: str, trigger_config: str) -> str:
        assert engine_state["finished"], "_calc_next_run must run after the engine completes"
        return "scheduled-after-completion"

    monkeypatch.setattr(
        auto_task_service_module,
        "load_runtime_identity_catalog",
        lambda: SimpleNamespace(resolve=lambda message: SimpleNamespace(identity_id="smith")),
    )
    monkeypatch.setattr(
        auto_task_service_module,
        "build_engine_runtime",
        lambda *args, **kwargs: (object(), object()),
    )
    monkeypatch.setattr(auto_task_service_module, "engine_reply_with_runtime", slow_reply)
    monkeypatch.setattr(
        auto_task_service_module.AutoTaskService,
        "_calc_next_run",
        staticmethod(fake_calc_next_run),
    )

    task_repo = FakeAutoTaskRepo()
    service = AutoTaskService(task_repo, FakeProfileRepo(), FakeSessionRepo())
    finished = await _run_to_completion(
        service,
        _task(trigger_type="interval", trigger_config="3600"),
    )

    assert finished["status"] == "completed"
    task_update = next(
        update for update in task_repo.updates if update.get("task_id") == "task-1"
    )
    assert task_update["next_run_at"] == "scheduled-after-completion"


@pytest.mark.asyncio
async def test_schedule_edited_mid_run_takes_effect_on_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trigger_type/config are snapshotted at claim time; a mid-run edit must
    still be honored on completion, not overwritten from the stale snapshot."""
    class EditedMidRunRepo(FakeAutoTaskRepo):
        async def get(self, task_id: str) -> dict:
            # The user switched the task to manual after it was claimed and while
            # the engine turn was still running.
            return _task(task_id, trigger_type="manual", trigger_config="")

    async def ok_reply(request, runtime, services):
        return SimpleNamespace(text="done")

    monkeypatch.setattr(
        auto_task_service_module, "load_runtime_identity_catalog",
        lambda: SimpleNamespace(resolve=lambda message: SimpleNamespace(identity_id="smith")),
    )
    monkeypatch.setattr(
        auto_task_service_module, "build_engine_runtime",
        lambda *args, **kwargs: (object(), object()),
    )
    monkeypatch.setattr(auto_task_service_module, "engine_reply_with_runtime", ok_reply)

    task_repo = EditedMidRunRepo()
    service = AutoTaskService(task_repo, FakeProfileRepo(), FakeSessionRepo())
    # Claim-time config is a live interval; the edit above switches it to manual.
    finished = await _run_to_completion(
        service, _task(trigger_type="interval", trigger_config="3600")
    )

    assert finished["status"] == "completed"
    task_update = next(u for u in task_repo.updates if u.get("task_id") == "task-1")
    # Honoring the edit means the task stops (manual → no next run); the stale
    # interval snapshot would have scheduled a concrete next_run_at.
    assert task_update["next_run_at"] is None


@pytest.mark.asyncio
async def test_hung_engine_run_times_out_and_releases_the_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pathological run must not renew its lease forever: an overall timeout
    turns it into a failed run so the concurrency slot frees up."""
    async def hung_reply(request, runtime, services):
        await asyncio.sleep(60)
        return SimpleNamespace(text="never")

    monkeypatch.setattr(
        auto_task_service_module,
        "load_runtime_identity_catalog",
        lambda: SimpleNamespace(resolve=lambda message: SimpleNamespace(identity_id="smith")),
    )
    monkeypatch.setattr(
        auto_task_service_module,
        "build_engine_runtime",
        lambda *args, **kwargs: (object(), object()),
    )
    monkeypatch.setattr(auto_task_service_module, "engine_reply_with_runtime", hung_reply)
    monkeypatch.setattr(auto_task_service_module, "_TASK_EXECUTION_TIMEOUT_SECONDS", 0.01)

    task_repo = FakeAutoTaskRepo()
    service = AutoTaskService(task_repo, FakeProfileRepo(), FakeSessionRepo())
    finished = await _run_to_completion(service, _task())

    assert finished["status"] == "failed"
    assert "timed out" in (finished["error"] or "").lower()


@pytest.mark.asyncio
async def test_manual_trigger_respects_the_concurrency_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manual trigger must not start more detached runs than _MAX_CONCURRENT_RUNS.

    The cap was enforced only in tick(); the /trigger path bypassed it, so a
    caller could loop over N tasks and run N engine turns with no bound.
    """
    release = asyncio.Event()

    async def blocking_reply(request, runtime, services):
        await release.wait()
        return SimpleNamespace(text="done")

    _stub_engine(monkeypatch, blocking_reply)
    task_repo = FakeAutoTaskRepo()
    service = AutoTaskService(task_repo, FakeProfileRepo(), FakeSessionRepo())

    started = []
    for _ in range(auto_task_service_module._MAX_CONCURRENT_RUNS):
        started.append(await service.start_auto_task(_task()))
    assert all(item is not None for item in started)
    # At the cap, start_auto_task must refuse rather than spawn a 5th run.
    assert await service.start_auto_task(_task()) is None

    release.set()
    await _drain_background_runs()


@pytest.mark.asyncio
async def test_trigger_returns_429_when_at_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()

    async def blocking_reply(request, runtime, services):
        await release.wait()
        return SimpleNamespace(text="done")

    _stub_engine(monkeypatch, blocking_reply)
    task_repo = FakeAutoTaskRepo()
    service = AutoTaskService(task_repo, FakeProfileRepo(), FakeSessionRepo())

    for _ in range(auto_task_service_module._MAX_CONCURRENT_RUNS):
        assert await service.start_auto_task(_task()) is not None

    with pytest.raises(HTTPException) as exc_info:
        await service.trigger_auto_task("smith-id", "task-1")
    assert exc_info.value.status_code == 429

    release.set()
    await _drain_background_runs()


@pytest.mark.asyncio
async def test_concurrent_starts_respect_the_cap_without_a_toctou_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N concurrent start_auto_task calls must not all start when N > cap.

    The cap check used to run before an await (the DB claim), so every
    concurrent caller saw an empty registry and started a run.  The slot is now
    reserved synchronously before the first await, closing the TOCTOU window.
    """
    release = asyncio.Event()
    claim_gate = asyncio.Event()
    claim_count = 0

    class GatedRepo(FakeAutoTaskRepo):
        async def claim_running(self, task_id: str) -> str | None:
            nonlocal claim_count
            claim_count += 1
            await claim_gate.wait()  # widen the race window
            return "lease-token"

    async def blocking_reply(request, runtime, services):
        await release.wait()
        return SimpleNamespace(text="done")

    _stub_engine(monkeypatch, blocking_reply)
    service = AutoTaskService(GatedRepo(), FakeProfileRepo(), FakeSessionRepo())

    tasks = [
        asyncio.create_task(
            service.start_auto_task(_task(task_id=f"task-{index}"))
        )
        for index in range(2 * auto_task_service_module._MAX_CONCURRENT_RUNS)
    ]
    await asyncio.sleep(0)  # let every task reach (or fail) the slot reservation
    claim_gate.set()
    results = await asyncio.gather(*tasks)

    started = [result for result in results if result is not None]
    assert len(started) <= auto_task_service_module._MAX_CONCURRENT_RUNS
    assert claim_count <= auto_task_service_module._MAX_CONCURRENT_RUNS

    release.set()
    await _drain_background_runs()


@pytest.mark.asyncio
async def test_reserved_slots_drain_when_starts_are_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a start_auto_task in the middle of the claim must release the
    reserved slot; a leaked reservation would permanently shrink the cap."""
    claim_gate = asyncio.Event()

    class GatedRepo(FakeAutoTaskRepo):
        async def claim_running(self, task_id: str) -> str | None:
            await claim_gate.wait()
            return "lease-token"

    async def blocking_reply(request, runtime, services):
        await asyncio.sleep(0.01)
        return SimpleNamespace(text="done")

    _stub_engine(monkeypatch, blocking_reply)
    service = AutoTaskService(GatedRepo(), FakeProfileRepo(), FakeSessionRepo())

    tasks = [
        asyncio.create_task(service.start_auto_task(_task(task_id=f"task-{index}")))
        for index in range(auto_task_service_module._MAX_CONCURRENT_RUNS)
    ]
    await asyncio.sleep(0)  # all reach the claim await and hold a reservation
    assert auto_task_service_module._RESERVED_SLOTS == auto_task_service_module._MAX_CONCURRENT_RUNS

    # The cap is fully reserved, so a new start refuses.
    assert await service.start_auto_task(_task(task_id="overflow")) is None

    # Cancel the in-flight starts; every finally must release its reservation.
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert auto_task_service_module._RESERVED_SLOTS == 0

    # A subsequent start succeeds once the slots drained.
    claim_gate.set()
    assert await service.start_auto_task(_task(task_id="after-drain")) is not None
    await _drain_background_runs()
    assert auto_task_service_module._RESERVED_SLOTS == 0
