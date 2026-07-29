from __future__ import annotations

import asyncio

from engine.tool.ledger import ToolExecutionLedger
from engine.tool.interface import ToolCall, ToolResult
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


def test_idempotent_retry_cannot_steal_a_running_claim(tmp_path):
    ledger = ToolExecutionLedger(tmp_path, "run-1")

    first = ledger.begin(
        call_id="call-1",
        tool_name="writer",
        idempotency_key="stable-key",
        idempotent=True,
    )
    second = ledger.begin(
        call_id="call-2",
        tool_name="writer",
        idempotency_key="stable-key",
        idempotent=True,
    )

    assert first.claimed
    assert not second.claimed
    assert second.result is not None
    assert second.result.error_kind == "side_effect_uncertain"


def test_stale_ledger_owner_cannot_finish_a_reclaimed_call(tmp_path):
    ledger = ToolExecutionLedger(tmp_path, "run-1")
    ledger.begin(
        call_id="call-1",
        tool_name="writer",
        idempotency_key="stable-key",
    )
    ledger.finish(
        call_id="call-1",
        idempotency_key="stable-key",
        result=ToolResult(
            call_id="call-1",
            content="first failed",
            is_error=True,
        ),
    )
    retry = ledger.begin(
        call_id="call-2",
        tool_name="writer",
        idempotency_key="stable-key",
        idempotent=True,
    )
    assert retry.claimed

    ledger.finish(
        call_id="call-1",
        idempotency_key="stable-key",
        result=ToolResult(call_id="call-1", content="stale success"),
    )
    while_retry_running = ledger.begin(
        call_id="call-3",
        tool_name="writer",
        idempotency_key="stable-key",
        idempotent=True,
    )
    assert not while_retry_running.claimed
    assert while_retry_running.result is not None
    assert while_retry_running.result.error_kind == "side_effect_uncertain"

    ledger.finish(
        call_id="call-2",
        idempotency_key="stable-key",
        result=ToolResult(call_id="call-2", content="retry success"),
    )
    replayed = ledger.begin(
        call_id="call-4",
        tool_name="writer",
        idempotency_key="stable-key",
        idempotent=True,
    )
    assert not replayed.claimed
    assert replayed.result is not None
    assert replayed.result.content == "retry success"
