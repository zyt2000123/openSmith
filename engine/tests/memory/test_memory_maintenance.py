from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import engine.memory.maintenance as maintenance_module
from engine.memory.maintenance import (
    MemoryLifecycleHooks,
    MemoryMaintenanceService,
)
from engine.memory.nudge import (
    MemoryNudgeError,
    NudgeEvidence,
    _load_evidence,
    _parse_candidates,
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


class NudgeLLM(StaticLLM):
    """Return a strict periodic-nudge decision while retaining compiler fixtures."""

    def __init__(self, decision: str, *, durable_text: str | None = None) -> None:
        super().__init__()
        self.decision = decision
        self.durable_text = durable_text

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        prefix_cache_key: str | None = None,
    ) -> ChatResponse:
        if "Periodic Memory Nudge" in messages[-1]["content"]:
            self.messages.append(messages)
            return ChatResponse(text=self.decision)
        if self.durable_text is not None and "`memory/durable.md`" in messages[-1]["content"]:
            self.messages.append(messages)
            return ChatResponse(text=self.durable_text)
        return await super().chat(messages, tools, prefix_cache_key)


class FailingNudgeLLM(StaticLLM):
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        prefix_cache_key: str | None = None,
    ) -> ChatResponse:
        if "Periodic Memory Nudge" in messages[-1]["content"]:
            raise RuntimeError("nudge provider unavailable")
        return await super().chat(messages, tools, prefix_cache_key)


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


def test_periodic_nudge_runs_after_twenty_events_and_records_no_candidate(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / ".nudge_counter").write_text("19", encoding="utf-8")

    result = asyncio.run(
        MemoryMaintenanceService(
            NudgeLLM('{"candidates": []}'),
            reviewer=PassReviewer(),
        ).record_turn(
            tmp_path,
            "Run the project test suite",
            "pytest -q passed: 141 passed",
            had_tools=True,
        )
    )

    assert result is True
    assert (memory_dir / ".nudge_counter").read_text(encoding="utf-8") == "0"
    history = [
        json.loads(line)
        for line in (memory_dir / "memory_history.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert history[-1]["target"] == "nudge"
    assert history[-1]["status"] == "unchanged"
    events = [
        json.loads(line)
        for line in (memory_dir / "recent.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert [event["kind"] for event in events] == ["work"]
    assert not (memory_dir / "durable.md").exists()


def test_periodic_nudge_candidate_enters_the_existing_compilation_pipeline(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / ".nudge_counter").write_text("19", encoding="utf-8")
    evidence = (
        "Engine memory tests require pytest-asyncio; the scoped suite passed "
        "when that dependency was present."
    )
    decision = json.dumps({
        "candidates": [{
            "kind": "procedure",
            "scope": "project",
            "content": "Use pytest-asyncio when running the Engine memory tests.",
            "evidence": evidence,
            "evidence_type": "tool_result",
        }],
    })
    durable_text = """# Durable Project Memory

## Confirmed Facts

## Decisions

## Reusable Procedures
- **Engine memory tests**: Use pytest-asyncio when running the Engine memory tests.

## Known Pitfalls
"""
    llm = NudgeLLM(decision, durable_text=durable_text)

    result = asyncio.run(
        MemoryMaintenanceService(
            llm,
            reviewer=PassReviewer(),
        ).record_turn(
            tmp_path,
            "Run the project test suite",
            evidence,
            had_tools=True,
        )
    )

    assert result is True
    events = [
        json.loads(line)
        for line in (memory_dir / "recent.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    candidate = events[-1]
    assert candidate["kind"] == "procedure"
    assert candidate["scope"] == "project"
    assert candidate["origin"] == "periodic_nudge"
    assert (memory_dir / "durable.md").read_text(encoding="utf-8") == durable_text
    assert (memory_dir / ".nudge_offset").read_text(encoding="utf-8") == "1"
    history = [
        json.loads(line)
        for line in (memory_dir / "memory_history.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert any(entry["target"] == "nudge" and entry["status"] == "written" for entry in history)
    assert any(entry["target"] == "durable" for entry in history)


def test_periodic_nudge_retry_deduplicates_a_partially_appended_candidate(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    evidence = "Engine memory tests require pytest-asyncio before the suite can pass."
    candidate_content = "Use pytest-asyncio when running the Engine memory tests."
    decision = json.dumps({
        "candidates": [{
            "kind": "procedure",
            "scope": "project",
            "content": candidate_content,
            "evidence": evidence,
            "evidence_type": "tool_result",
        }],
    })
    events = [
        {
            "task": "Run the project test suite",
            "summary": evidence,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "kind": "work",
            "scope": "project",
            "evidence": "tool_result",
        },
        {
            "task": f"[nudge] {candidate_content}",
            "summary": evidence,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "kind": "procedure",
            "scope": "project",
            "evidence": "tool_result",
            "evidence_type": "tool_result",
            "origin": "periodic_nudge",
        },
    ]
    (memory_dir / "recent.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    (memory_dir / ".nudge_counter").write_text("20", encoding="utf-8")

    result = asyncio.run(
        MemoryMaintenanceService(
            NudgeLLM(decision),
            reviewer=PassReviewer(),
        ).run_idle_maintenance(memory_dir)
    )

    assert result is True
    stored = [
        json.loads(line)
        for line in (memory_dir / "recent.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert len(stored) == 2
    assert sum(event.get("origin") == "periodic_nudge" for event in stored) == 1
    assert (memory_dir / ".nudge_offset").read_text(encoding="utf-8") == "2"
    assert (memory_dir / ".nudge_counter").read_text(encoding="utf-8") == "0"


def test_periodic_nudge_rejects_transient_task_state_even_if_reviewer_passes(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / ".nudge_counter").write_text("19", encoding="utf-8")
    evidence = "The remaining Engine tests still need to be run before release."
    decision = json.dumps({
        "candidates": [{
            "kind": "procedure",
            "scope": "project",
            "content": "Next step: run the remaining Engine tests.",
            "evidence": evidence,
            "evidence_type": "tool_result",
        }],
    })

    result = asyncio.run(
        MemoryMaintenanceService(
            NudgeLLM(decision),
            reviewer=PassReviewer(),
        ).record_turn(
            tmp_path,
            "Check the release state",
            evidence,
            had_tools=True,
        )
    )

    assert result is True
    assert (memory_dir / ".nudge_counter").read_text(encoding="utf-8") == "20"
    history = [
        json.loads(line)
        for line in (memory_dir / "memory_history.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert history[-1]["target"] == "nudge"
    assert history[-1]["status"] == "rejected"
    events = [
        json.loads(line)
        for line in (memory_dir / "recent.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert [event["kind"] for event in events] == ["work"]
    assert not (memory_dir / "durable.md").exists()


def test_periodic_nudge_failure_keeps_the_due_counter_for_retry(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / ".nudge_counter").write_text("19", encoding="utf-8")

    result = asyncio.run(
        MemoryMaintenanceService(
            FailingNudgeLLM(),
            reviewer=PassReviewer(),
        ).record_turn(
            tmp_path,
            "Run the project test suite",
            "pytest -q passed: 141 passed",
            had_tools=True,
        )
    )

    assert result is True
    assert (memory_dir / ".nudge_counter").read_text(encoding="utf-8") == "20"
    history = [
        json.loads(line)
        for line in (memory_dir / "memory_history.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert history[-1]["target"] == "nudge"
    assert history[-1]["status"] == "failed"


def test_periodic_nudge_rejects_an_unsafe_offset_checkpoint(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    external_offset = tmp_path / "outside-offset"
    external_offset.write_text("0", encoding="utf-8")
    (memory_dir / ".nudge_offset").symlink_to(external_offset)
    (memory_dir / ".nudge_counter").write_text("19", encoding="utf-8")

    result = asyncio.run(
        MemoryMaintenanceService(
            NudgeLLM('{"candidates": []}'),
            reviewer=PassReviewer(),
        ).record_turn(
            tmp_path,
            "Run the project test suite",
            "pytest -q passed: 141 passed",
            had_tools=True,
        )
    )

    assert result is True
    assert external_offset.read_text(encoding="utf-8") == "0"
    assert (memory_dir / ".nudge_counter").read_text(encoding="utf-8") == "20"
    history = [
        json.loads(line)
        for line in (memory_dir / "memory_history.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert history[-1]["target"] == "nudge"
    assert history[-1]["status"] == "failed"


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


def test_dream_maintenance_retries_when_durable_target_is_unavailable(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    event = (
        '{"task":"durable correction","summary":"Correct the durable fact.",'
        '"timestamp":"2026-07-28T00:00:00+00:00","kind":"decision",'
        '"scope":"project","evidence":"user_explicit"}\n'
    )
    (memory_dir / "recent.jsonl").write_text(event, encoding="utf-8")
    (memory_dir / ".dream_counter").write_text("50", encoding="utf-8")

    result = asyncio.run(MemoryMaintenanceService(StaticLLM()).run_dream(memory_dir))  # type: ignore[arg-type]

    assert result is False
    assert (memory_dir / ".dream_counter").read_text(encoding="utf-8") == "50"
    assert (memory_dir / "recent.jsonl").read_text(encoding="utf-8") == event
    assert not (memory_dir / ".dream_offset").exists()


def test_dream_maintenance_completes_the_reconciliation_cycle(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    original = """# Durable Project Memory

## Confirmed Facts
- **Storage**: Keep the original storage decision for this project.

## Decisions

## Reusable Procedures

## Known Pitfalls
"""
    replacement = """# Durable Project Memory

## Confirmed Facts
- **Storage**: Use the corrected storage decision for this project.

## Decisions

## Reusable Procedures

## Known Pitfalls
"""
    (memory_dir / "durable.md").write_text(original, encoding="utf-8")
    (memory_dir / "recent.jsonl").write_text(
        '{"task":"storage correction","summary":"Use the corrected storage decision '
        'for this project.","timestamp":"2026-07-28T00:00:00+00:00",'
        '"kind":"decision","scope":"project","evidence":"user_explicit"}\n',
        encoding="utf-8",
    )
    (memory_dir / ".dream_counter").write_text("50", encoding="utf-8")

    result = asyncio.run(
        MemoryMaintenanceService(
            StaticLLM(replacement),
            reviewer=PassReviewer(),
        ).run_dream(memory_dir)
    )

    assert result is True
    assert (memory_dir / "durable.md").read_text(encoding="utf-8") == replacement
    assert (memory_dir / ".dream_offset").read_text(encoding="utf-8") == "1"
    assert (memory_dir / ".dream_counter").read_text(encoding="utf-8") == "0"


def test_dream_maintenance_times_out_and_preserves_retry_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    durable = """# Durable Project Memory

## Confirmed Facts
- **Storage**: Keep an accepted storage decision for this project with enough detail.

## Decisions

## Reusable Procedures

## Known Pitfalls
"""
    (memory_dir / "durable.md").write_text(durable, encoding="utf-8")
    (memory_dir / "recent.jsonl").write_text(
        '{"task":"storage correction","summary":"Correct the storage decision.",'
        '"timestamp":"2026-07-28T00:00:00+00:00","kind":"decision",'
        '"scope":"project","evidence":"user_explicit"}\n',
        encoding="utf-8",
    )
    (memory_dir / ".dream_counter").write_text("50", encoding="utf-8")
    monkeypatch.setattr(
        maintenance_module,
        "_MEMORY_MAINTENANCE_TIMEOUT_SECONDS",
        0.01,
    )

    class HangingLLM:
        async def chat(self, *args, **kwargs) -> ChatResponse:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def run() -> bool:
        service = MemoryMaintenanceService(HangingLLM(), reviewer=PassReviewer())  # type: ignore[arg-type]
        return await asyncio.wait_for(service.run_dream(memory_dir), timeout=0.1)

    assert asyncio.run(run()) is False
    assert (memory_dir / ".dream_counter").read_text(encoding="utf-8") == "50"
    assert not (memory_dir / ".dream_offset").exists()
    history = [
        json.loads(line)
        for line in (memory_dir / "memory_history.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert history[-1]["target"] == "dream"
    assert history[-1]["status"] == "failed"
    assert "TimeoutError" in history[-1]["error"]


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
        "nudge": "idle",
        "dream": "idle",
        "topic_sync": "idle",
        "consecutive_failures": 0,
        "last_error": None,
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
    assert status["nudge"] == "idle"


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


def test_maintenance_status_surfaces_a_trailing_failure_streak(tmp_path):
    """A pipeline stalled on provider errors must be visible, not just logged."""
    from engine.memory import memory_maintenance_status
    from engine.memory.history import append_memory_history

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    append_memory_history(memory_dir, target="recent", policy_version=1, status="written")
    for _ in range(3):
        append_memory_history(
            memory_dir,
            target="recent",
            policy_version=1,
            status="failed",
            error="LLMResponseError: LLM request failed (HTTP 401) after 1 attempt(s).",
        )

    status = memory_maintenance_status(memory_dir)

    assert status["consecutive_failures"] == 3
    assert "401" in status["last_error"]


def test_failure_streak_ends_at_the_newest_successful_operation(tmp_path):
    from engine.memory import memory_maintenance_status
    from engine.memory.history import append_memory_history

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    append_memory_history(
        memory_dir, target="recent", policy_version=1, status="failed", error="boom"
    )
    append_memory_history(memory_dir, target="recent", policy_version=1, status="written")

    status = memory_maintenance_status(memory_dir)

    assert status["consecutive_failures"] == 0
    assert status["last_error"] is None


# ---------------------------------------------------------------------------
# Maintenance retry backoff
# ---------------------------------------------------------------------------

_DREAM_DOC = """# Durable Project Memory

## Confirmed Facts
- **Storage**: {detail}

## Decisions

## Reusable Procedures

## Known Pitfalls
"""


def test_dream_maintenance_backs_off_after_a_transport_failure(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    original = _DREAM_DOC.format(
        detail="Keep the original storage decision for this project with enough detail."
    )
    (memory_dir / "durable.md").write_text(original, encoding="utf-8")
    (memory_dir / "recent.jsonl").write_text(
        json.dumps({
            "task": "storage correction",
            "summary": "Correct the storage decision.",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "kind": "decision",
            "scope": "project",
            "evidence": "user_explicit",
        }) + "\n",
        encoding="utf-8",
    )
    (memory_dir / ".dream_counter").write_text("50", encoding="utf-8")
    calls = 0

    class FlakyLLM:
        async def chat(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            raise RuntimeError("provider unavailable")

    async def run() -> tuple[bool, bool]:
        service = MemoryMaintenanceService(FlakyLLM(), reviewer=PassReviewer())
        first = await service.run_dream(memory_dir)
        second = await service.run_dream(memory_dir)
        return first, second

    first, second = asyncio.run(run())

    assert (first, second) == (False, False)
    assert calls == 1  # the second attempt is inside the cooldown
    assert (memory_dir / ".dream_retry_attempt").is_file()


def test_dream_maintenance_retries_review_rejections_without_backoff(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    original = _DREAM_DOC.format(
        detail="Keep the original storage decision for this project with enough detail."
    )
    (memory_dir / "durable.md").write_text(original, encoding="utf-8")
    (memory_dir / "recent.jsonl").write_text(
        json.dumps({
            "task": "storage correction",
            "summary": "Correct the storage decision.",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "kind": "decision",
            "scope": "project",
            "evidence": "user_explicit",
        }) + "\n",
        encoding="utf-8",
    )
    (memory_dir / ".dream_counter").write_text("50", encoding="utf-8")
    calls = 0

    class CountingLLM:
        async def chat(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            return ChatResponse(text=original)

    class RejectReviewer:
        async def chat(self, messages, **_):
            return ChatResponse(
                text='{"pass": false, "hard_fail": ["fabrication"], "soft_fail": [], "feedback": "bad"}'
            )

        async def close(self):
            pass

    async def run() -> tuple[bool, bool]:
        service = MemoryMaintenanceService(
            CountingLLM(),
            reviewer=RejectReviewer(),  # type: ignore[arg-type]
        )
        first = await service.run_dream(memory_dir)
        second = await service.run_dream(memory_dir)
        return first, second

    first, second = asyncio.run(run())

    assert (first, second) == (False, False)
    assert calls >= 6  # a full review round-trip on both attempts, no cooldown
    assert not (memory_dir / ".dream_retry_attempt").exists()


def test_save_conversation_memory_skips_due_dream_lane_inside_cooldown(
    tmp_path: Path,
) -> None:
    from engine.memory.store import save_conversation_memory

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / ".dream_counter").write_text("50", encoding="utf-8")
    (memory_dir / ".dream_retry_attempt").write_text(str(time.time()), encoding="utf-8")

    maintenance = AsyncMock(return_value=False)
    asyncio.run(save_conversation_memory(
        tmp_path,
        "task",
        "reply",
        had_tools=True,
        dream_maintenance=maintenance,
    ))

    maintenance.assert_not_awaited()


def test_deferred_schedule_respects_dream_cooldown(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / ".dream_retry_attempt").write_text(str(time.time()), encoding="utf-8")

    service = MemoryMaintenanceService(
        StaticLLM(),
        reviewer=PassReviewer(),
        defer_maintenance=True,
    )
    assert asyncio.run(service._schedule_dream(memory_dir)) is False
    assert not any(
        path == memory_dir.resolve() and kind == "dream"
        for (path, kind) in MemoryMaintenanceService._background_tasks
    )


# ---------------------------------------------------------------------------
# Background-task bookkeeping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_background_task_finish_does_not_evict_a_new_registration(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    key = (memory_dir.resolve(), "compile")

    async def quick() -> None:
        return None

    old_task = asyncio.create_task(quick())
    await old_task
    new_task = asyncio.create_task(asyncio.Event().wait())
    MemoryMaintenanceService._background_tasks[key] = new_task

    MemoryMaintenanceService._discard_completed_task(key, old_task)

    assert MemoryMaintenanceService._background_tasks.get(key) is new_task
    new_task.cancel()
    try:
        await new_task
    except asyncio.CancelledError:
        pass
    MemoryMaintenanceService._background_tasks.pop(key, None)


@pytest.mark.asyncio
async def test_background_task_finish_pops_only_the_registered_completed_task(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    key = (memory_dir.resolve(), "nudge")

    async def quick() -> None:
        return None

    task = asyncio.create_task(quick())
    await task
    MemoryMaintenanceService._background_tasks[key] = task

    MemoryMaintenanceService._discard_completed_task(key, task)

    assert key not in MemoryMaintenanceService._background_tasks


# ---------------------------------------------------------------------------
# Audit-history retention
# ---------------------------------------------------------------------------

def test_trim_memory_history_keeps_recent_entries(tmp_path: Path) -> None:
    from engine.memory.history import trim_memory_history

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    history_path = memory_dir / "memory_history.jsonl"
    stale = json.dumps({
        "timestamp": "2020-01-01T00:00:00+00:00",
        "target": "durable",
        "status": "written",
    })
    recent = json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "target": "durable",
        "status": "written",
    })
    history_path.write_text(stale + "\n" + recent + "\n", encoding="utf-8")

    assert trim_memory_history(memory_dir) is True
    remaining = history_path.read_text(encoding="utf-8").splitlines()
    assert len(remaining) == 1
    assert "2020-01-01" not in remaining[0]


def test_trim_memory_history_caps_entry_count(tmp_path: Path) -> None:
    from engine.memory.history import trim_memory_history

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    now = datetime.now(timezone.utc)
    lines = [
        json.dumps({
            "timestamp": (now - timedelta(minutes=index)).isoformat(),
            "target": "dream",
            "status": "written",
        })
        for index in range(60)
    ]
    (memory_dir / "memory_history.jsonl").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    assert trim_memory_history(memory_dir, max_entries=10) is True
    remaining = (memory_dir / "memory_history.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(remaining) == 10


def test_dream_maintenance_trims_stale_audit_history(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    stale = json.dumps({
        "timestamp": "2020-01-01T00:00:00+00:00",
        "target": "dream",
        "status": "written",
    })
    (memory_dir / "memory_history.jsonl").write_text(stale + "\n", encoding="utf-8")
    original = _DREAM_DOC.format(
        detail="Keep the original storage decision for this project with enough detail."
    )
    replacement = _DREAM_DOC.format(
        detail="Use the corrected storage decision for this project with enough detail."
    )
    (memory_dir / "durable.md").write_text(original, encoding="utf-8")
    (memory_dir / "recent.jsonl").write_text(
        json.dumps({
            "task": "storage correction",
            "summary": "Use the corrected storage decision for this project.",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "kind": "decision",
            "scope": "project",
            "evidence": "user_explicit",
        }) + "\n",
        encoding="utf-8",
    )

    result = asyncio.run(
        MemoryMaintenanceService(
            StaticLLM(replacement),
            reviewer=PassReviewer(),
        ).run_dream(memory_dir)
    )

    assert result is True
    remaining = (memory_dir / "memory_history.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert remaining
    assert all("2020-01-01" not in line for line in remaining)


# ---------------------------------------------------------------------------
# Nudge candidate handling
# ---------------------------------------------------------------------------

def test_nudge_load_evidence_repairs_a_torn_trailing_line(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    good = json.dumps({
        "task": "Run tests",
        "summary": "pytest passed 141 tests",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "kind": "work",
        "scope": "project",
        "evidence": "tool_result",
    })
    (memory_dir / "recent.jsonl").write_text(
        good + "\n" + '{"task": "torn',
        encoding="utf-8",
    )

    evidence = _load_evidence(memory_dir)

    assert evidence.error is None
    assert evidence.end_offset == 1
    assert (memory_dir / "recent.jsonl").read_text(encoding="utf-8") == good + "\n"


def test_nudge_rejects_an_ambiguous_evidence_excerpt() -> None:
    evidence = NudgeEvidence(
        start_offset=0,
        end_offset=2,
        source=(
            "[Evidence 1]\nTask: a\nResult: pytest passed 141 tests\n"
            "Evidence type: tool_result\n\n"
            "[Evidence 2]\nTask: b\nResult: pytest passed 141 tests\n"
            "Evidence type: tool_result"
        ),
        excerpts=("pytest passed 141 tests", "pytest passed 141 tests"),
    )
    payload = json.dumps({
        "candidates": [{
            "kind": "procedure",
            "scope": "project",
            "content": "Use pytest for testing",
            "evidence": "passed 141 tests",
            "evidence_type": "tool_result",
        }],
    })

    with pytest.raises(MemoryNudgeError, match="evidence"):
        _parse_candidates(payload, evidence)


def test_nudge_accepts_an_excerpt_bound_to_exactly_one_event() -> None:
    evidence = NudgeEvidence(
        start_offset=0,
        end_offset=2,
        source=(
            "[Evidence 1]\nTask: a\nResult: pytest passed 141 tests\n"
            "Evidence type: tool_result\n\n"
            "[Evidence 2]\nTask: b\nResult: deploy happened on Friday\n"
            "Evidence type: tool_result"
        ),
        excerpts=("pytest passed 141 tests", "deploy happened on Friday"),
    )
    payload = json.dumps({
        "candidates": [{
            "kind": "procedure",
            "scope": "project",
            "content": "Use pytest for testing",
            "evidence": "pytest passed 141 tests",
            "evidence_type": "tool_result",
        }],
    })

    candidates = _parse_candidates(payload, evidence)
    assert len(candidates) == 1
    assert candidates[0].evidence == "pytest passed 141 tests"
