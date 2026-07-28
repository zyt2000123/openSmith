from __future__ import annotations

import asyncio
import multiprocessing
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from engine.memory.maintenance import (
    MemoryLifecycleHooks,
    MemoryMaintenanceService,
)
from engine.execution.orchestration.lifecycle import (
    _ensure_memory_lifecycle_hooks,
    run_memory_idle_tick,
)
from engine.execution.orchestration.runtime import RuntimeServices
from engine.execution.hooks import HookManager, HookType
from engine.llm.client import ChatResponse
from engine.skill.registry import SkillRegistry
from engine.tool.registry import ToolRegistry


class StaticLLM:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.messages: list[list[dict]] = []

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        prefix_cache_key: str | None = None,
    ) -> ChatResponse:
        self.messages.append(messages)
        if self.text is not None:
            return ChatResponse(text=self.text)
        prompt = messages[-1]["content"]
        if "`memory/recent.md`" in prompt:
            return ChatResponse(text="""# Recent Working Memory

## Active Work
- **Hook test** — 状态：active；下一步：verify；更新：2026-07-13。

## Pending

## Recent Verified Outcomes
""")
        if "`memory/durable.md`" in prompt:
            return ChatResponse(text="""# Durable Project Memory

## Confirmed Facts
- **Hook test**: The memory hook records tool-assisted work.

## Decisions

## Reusable Procedures

## Known Pitfalls
""")
        if "`context.md`" in prompt:
            return ChatResponse(text="""# Smith Context

## Confirmed Preferences
- **Memory**: Honor explicit remember requests.

## Collaboration Patterns

## Stable User Context
""")
        return ChatResponse(text="stable memory summary")


class PassReviewer(StaticLLM):
    def __init__(self) -> None:
        super().__init__(
            '{"pass": true, "hard_fail": [], "soft_fail": [], "feedback": ""}'
        )


def _record_turn_in_process(agent_dir: str, start) -> None:
    original_read_text = Path.read_text

    def delayed_counter_read(path: Path, *args, **kwargs) -> str:
        value = original_read_text(path, *args, **kwargs)
        if path.name == ".compile_counter":
            time.sleep(0.05)
        return value

    Path.read_text = delayed_counter_read  # type: ignore[method-assign]
    start.wait(timeout=5)
    result = asyncio.run(
        MemoryMaintenanceService(
            StaticLLM(),
            defer_maintenance=True,
        ).record_turn(
            Path(agent_dir),
            f"task-{os.getpid()}",
            "verified reply",
            had_tools=True,
        )
    )
    if not result:
        raise RuntimeError("record_turn failed")


@pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(),
    reason="requires a POSIX fork context",
)
def test_record_turn_serializes_counter_updates_across_processes(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / ".compile_counter").write_text("0", encoding="utf-8")
    (memory_dir / ".dream_counter").write_text("0", encoding="utf-8")
    process_count = 4
    context = multiprocessing.get_context("fork")
    start = context.Barrier(process_count)
    processes = [
        context.Process(
            target=_record_turn_in_process,
            args=(str(tmp_path), start),
        )
        for _ in range(process_count)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)

    assert [process.exitcode for process in processes] == [0] * process_count
    assert (memory_dir / ".compile_counter").read_text(encoding="utf-8") == "4"
    assert len(
        (memory_dir / "recent.jsonl").read_text(encoding="utf-8").splitlines()
    ) == process_count


def test_memory_after_turn_hook_records_and_compiles(tmp_path: Path) -> None:
    async def run() -> tuple[list[bool], StaticLLM]:
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / ".compile_counter").write_text("4", encoding="utf-8")
        llm = StaticLLM()
        hooks = HookManager()
        hooks.register(MemoryLifecycleHooks(
            MemoryMaintenanceService(llm, reviewer=PassReviewer())  # type: ignore[arg-type]
        ))

        results = await hooks.apply(
            "memory_after_turn_completed",
            HookType.PARALLEL,
            args=(tmp_path, "remember this", "tool-assisted reply", True),
        )
        return results, llm

    results, llm = asyncio.run(run())

    memory_dir = tmp_path / "memory"
    assert results == [True]
    assert (memory_dir / "recent.jsonl").is_file()
    assert (memory_dir / "recent.md").is_file()
    assert (memory_dir / ".compile_counter").read_text(encoding="utf-8") == "0"
    assert llm.messages


def test_explicit_toolless_preference_compiles_context_immediately(tmp_path: Path) -> None:
    result = asyncio.run(
        MemoryMaintenanceService(
            StaticLLM(),
            reviewer=PassReviewer(),
        ).record_turn(
            tmp_path,
            "以后默认用中文回答",
            "好的",
            had_tools=False,
        )
    )

    assert result is True
    assert (tmp_path / "context.md").is_file()
    assert (tmp_path / "memory" / "recent.jsonl").is_file()
    assert (tmp_path / "memory" / ".compile_counter").read_text(
        encoding="utf-8"
    ) == "0"


def test_memory_idle_hook_uses_same_maintenance_service(tmp_path: Path) -> None:
    async def run() -> list[bool]:
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / "durable.md").write_text("small durable note", encoding="utf-8")
        hooks = HookManager()
        hooks.register(MemoryLifecycleHooks(MemoryMaintenanceService(StaticLLM())))  # type: ignore[arg-type]

        return await hooks.apply(
            "memory_idle_tick",
            HookType.PARALLEL,
            args=(memory_dir,),
        )

    results = asyncio.run(run())

    assert results == [True]


def test_idle_maintenance_retries_pending_work_without_running_below_threshold(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []

    async def fail_compile(self, memory_dir: Path) -> bool:
        calls.append("compile")
        return False

    async def run() -> tuple[bool, bool]:
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / ".compile_counter").write_text("5", encoding="utf-8")
        service = MemoryMaintenanceService(StaticLLM())  # type: ignore[arg-type]
        first = await service.run_idle_maintenance(memory_dir)
        # A failed, due compilation remains pending; a future idle tick retries it.
        second = await service.run_idle_maintenance(memory_dir)
        return first, second

    monkeypatch.setattr(MemoryMaintenanceService, "_run_compilation_unlocked", fail_compile)
    first, second = asyncio.run(run())

    assert (first, second) == (False, False)
    assert calls == ["compile", "compile"]


def test_idle_maintenance_skips_work_that_is_not_pending(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []

    async def unexpected_compile(self, memory_dir: Path) -> bool:
        calls.append("compile")
        return True

    async def unexpected_dream(self, memory_dir: Path) -> bool:
        calls.append("dream")
        return True

    async def run() -> bool:
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / ".compile_counter").write_text("1", encoding="utf-8")
        (memory_dir / ".dream_counter").write_text("1", encoding="utf-8")
        return await MemoryMaintenanceService(StaticLLM()).run_idle_maintenance(memory_dir)  # type: ignore[arg-type]

    monkeypatch.setattr(MemoryMaintenanceService, "_run_compilation_unlocked", unexpected_compile)
    monkeypatch.setattr(MemoryMaintenanceService, "_run_dream_unlocked", unexpected_dream)

    assert asyncio.run(run()) is True
    assert calls == []


def test_memory_compilation_timeout_does_not_block_lifecycle(tmp_path: Path, monkeypatch) -> None:
    import engine.memory.maintenance as memory_maintenance

    async def slow_compilation(*_args, **_kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(memory_maintenance, "_MEMORY_MAINTENANCE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("engine.memory.compile.run_compilation", slow_compilation)

    result = asyncio.run(
        MemoryMaintenanceService(StaticLLM()).run_compile(tmp_path / "memory")
    )

    assert result is False


def test_memory_compilation_reports_partial_progress(
    tmp_path: Path,
    monkeypatch,
) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "recent.jsonl").write_text(
        '{"task":"keep recent activity","summary":"recent evidence",'
        '"timestamp":"' + datetime.now(timezone.utc).isoformat() + '"}\n',
        encoding="utf-8",
    )

    async def fail_durable(*_args, **_kwargs):
        raise RuntimeError("reviewer unavailable")

    monkeypatch.setattr("engine.memory.compile.compile_durable", fail_durable)

    result = asyncio.run(
        MemoryMaintenanceService(
            StaticLLM(),
            reviewer=PassReviewer(),
        ).run_compile(memory_dir)
    )

    assert result is False
    assert (memory_dir / "recent.md").is_file()


def test_deferred_memory_maintenance_does_not_block_turn_and_can_be_drained(
    tmp_path: Path,
) -> None:
    async def run() -> tuple[bool, float, Path]:
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / ".compile_counter").write_text("4", encoding="utf-8")
        service = MemoryMaintenanceService(
            StaticLLM(),
            reviewer=PassReviewer(),
            defer_maintenance=True,
        )

        started = time.perf_counter()
        result = await service.record_turn(
            tmp_path,
            "tool task",
            "tool result",
            had_tools=True,
        )
        elapsed = time.perf_counter() - started
        await service.wait_for_pending_tasks(memory_dir)
        return result, elapsed, memory_dir

    result, elapsed, memory_dir = asyncio.run(run())

    assert result is True
    assert elapsed < 0.2
    assert (memory_dir / "recent.md").is_file()
    assert (memory_dir / ".compile_counter").read_text(encoding="utf-8") == "0"


def test_deferred_memory_maintenance_uses_background_llm(
    tmp_path: Path,
) -> None:
    async def run() -> tuple[StaticLLM, StaticLLM]:
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / ".compile_counter").write_text("4", encoding="utf-8")
        interactive = StaticLLM()
        background = StaticLLM()
        service = MemoryMaintenanceService(
            background,
            reviewer=PassReviewer(),
            defer_maintenance=True,
        )

        assert await service.record_turn(
            tmp_path,
            "tool task",
            "tool result",
            had_tools=True,
        ) is True
        await service.wait_for_pending_tasks(memory_dir)
        return interactive, background

    interactive, background = asyncio.run(run())

    assert not interactive.messages
    assert background.messages


def test_runtime_idle_tick_dispatches_memory_hook(tmp_path: Path) -> None:
    async def run() -> tuple[bool, RuntimeServices]:
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / "durable.md").write_text("small durable note", encoding="utf-8")
        services = RuntimeServices(
            llm=StaticLLM(),  # type: ignore[arg-type]
            tool_registry=ToolRegistry(),
            skill_registry=SkillRegistry(),
        )

        ok = await run_memory_idle_tick(memory_dir, services)
        return ok, services

    ok, services = asyncio.run(run())

    assert ok is True
    assert services.hooks is not None


def test_shared_runtime_defers_heavy_memory_maintenance(tmp_path: Path) -> None:
    background = StaticLLM()
    services = RuntimeServices(
        llm=StaticLLM(),  # type: ignore[arg-type]
        tool_registry=ToolRegistry(),
        skill_registry=SkillRegistry(),
        background_llm=background,  # type: ignore[arg-type]
        owns_llm_clients=False,
    )

    _ensure_memory_lifecycle_hooks(services)

    assert services.hooks is not None
    handler = services.hooks._handlers[0]
    assert handler.maintenance.defer_maintenance is True
    assert handler.maintenance.llm is background


def test_memory_hook_rebinds_when_runtime_dependencies_change() -> None:
    first = StaticLLM()
    second = StaticLLM()
    services = RuntimeServices(
        llm=StaticLLM(),  # type: ignore[arg-type]
        tool_registry=ToolRegistry(),
        skill_registry=SkillRegistry(),
        background_llm=first,  # type: ignore[arg-type]
    )

    _ensure_memory_lifecycle_hooks(services)
    services.background_llm = second  # type: ignore[assignment]
    _ensure_memory_lifecycle_hooks(services)

    assert services.hooks is not None
    assert len(services.hooks._handlers) == 1
    assert services.hooks._handlers[0].maintenance.llm is second


# ── Deferred maintenance must be observable (dreaming indicator) ──


def test_maintenance_status_reports_idle_for_a_fresh_memory_dir(tmp_path):
    from engine.memory import memory_maintenance_status

    (tmp_path / "memory").mkdir()

    assert memory_maintenance_status(tmp_path / "memory") == {
        "compile": "idle",
        "dream": "idle",
    }


def test_maintenance_status_reports_pending_from_the_marker_file(tmp_path):
    from engine.memory import memory_maintenance_status
    from engine.memory.maintenance import MemoryMaintenanceService

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    MemoryMaintenanceService._mark_pending("dream", memory_dir)

    status = memory_maintenance_status(memory_dir)

    assert status["dream"] == "pending"
    assert status["compile"] == "idle"


@pytest.mark.asyncio
async def test_maintenance_status_reports_running_while_a_task_is_in_flight(tmp_path):
    from engine.memory import memory_maintenance_status
    from engine.memory.maintenance import MemoryMaintenanceService

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    release = asyncio.Event()

    async def blocked() -> None:
        await release.wait()

    key = (memory_dir.resolve(), "compile")
    task = asyncio.create_task(blocked())
    MemoryMaintenanceService._background_tasks[key] = task
    try:
        assert memory_maintenance_status(memory_dir)["compile"] == "running"
    finally:
        release.set()
        await task
        MemoryMaintenanceService._background_tasks.pop(key, None)

    assert memory_maintenance_status(memory_dir)["compile"] == "idle"
