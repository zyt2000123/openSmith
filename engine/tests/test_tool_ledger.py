from __future__ import annotations

import asyncio

from engine.execution.tool_execution.tool_ledger import ToolExecutionLedger
from engine.tool.interface import ToolCall
from engine.tool.registry import ToolRegistry


def test_side_effect_tool_replays_completed_call_without_reexecution(tmp_path):
    calls: list[str] = []

    async def write_tool(value: str):
        calls.append(value)
        return f"written:{value}"

    async def run():
        registry = ToolRegistry()
        registry.register("writer", "", {}, write_tool, side_effect="write")
        registry.bind_execution_ledger(ToolExecutionLedger(tmp_path, "run-1"))
        first = await registry.execute(
            ToolCall(id="call-1", name="writer", arguments={"value": "x"})
        )
        second = await registry.execute(
            ToolCall(id="call-1", name="writer", arguments={"value": "x"})
        )
        return first, second

    first, second = asyncio.run(run())
    assert calls == ["x"]
    assert first.content == "written:x"
    assert second.content == "written:x"
    assert second.metadata["replayed"] is True


def test_uncertain_side_effect_is_not_retried_automatically(tmp_path):
    calls: list[str] = []

    async def flaky_writer():
        calls.append("called")
        raise RuntimeError("connection lost after write")

    async def run():
        registry = ToolRegistry()
        registry.register("writer", "", {}, flaky_writer, side_effect="write")
        registry.bind_execution_ledger(ToolExecutionLedger(tmp_path, "run-1"))
        first = await registry.execute(ToolCall(id="call-1", name="writer"))
        second = await registry.execute(ToolCall(id="call-1", name="writer"))
        return first, second

    first, second = asyncio.run(run())
    assert calls == ["called"]
    assert first.is_error is True
    assert first.side_effect_status == "unknown"
    assert second.error_kind == "side_effect_uncertain"
    assert second.retryable is False


def test_resumed_run_replays_side_effect_when_provider_call_id_changes(tmp_path):
    calls: list[str] = []

    async def write_tool(value: str):
        calls.append(value)
        return f"written:{value}"

    async def run():
        first_registry = ToolRegistry()
        first_registry.register("writer", "", {}, write_tool, side_effect="write")
        first_registry.bind_execution_ledger(ToolExecutionLedger(tmp_path, "run-1"))
        first = await first_registry.execute(
            ToolCall(id="provider-call-1", name="writer", arguments={"value": "x"})
        )

        resumed_registry = ToolRegistry()
        resumed_registry.register("writer", "", {}, write_tool, side_effect="write")
        resumed_registry.bind_execution_ledger(
            ToolExecutionLedger(tmp_path, "run-1", replay_existing=True)
        )
        replayed = await resumed_registry.execute(
            ToolCall(id="provider-call-after-resume", name="writer", arguments={"value": "x"})
        )
        return first, replayed

    first, replayed = asyncio.run(run())

    assert calls == ["x"]
    assert first.content == "written:x"
    assert replayed.content == "written:x"
    assert replayed.metadata["replayed"] is True
