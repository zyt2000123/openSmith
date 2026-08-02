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

    async def finish_run(self, run_id: str, status: str, output: str, error: str | None = None) -> dict:
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
