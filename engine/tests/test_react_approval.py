from __future__ import annotations

import asyncio
from pathlib import Path

from engine.observability.events import EventType
from engine.execution.react.react_loop import react_event_loop
from engine.llm.client import ChatResponse
from engine.llm.contracts import ToolCallData
from engine.safety.approval import (
    APPROVAL_BROKER,
    ApprovalBroker,
    ApprovalTimeoutError,
    use_approval_context,
)
from engine.safety.tool_guard import ToolGuard
from engine.tool.registry import ToolRegistry


def test_react_loop_executes_a_guarded_tool_only_after_approval(tmp_path: Path) -> None:
    async def run():
        target = tmp_path / "approval-test.txt"
        registry = ToolRegistry()

        async def write_file(path: str, content: str):
            Path(path).write_text(content, encoding="utf-8")
            return "written"

        registry.register(
            "write_file", "Write", {}, write_file,
            permission_level="write", approval_policy="policy", side_effect="write",
        )
        guard = ToolGuard(tmp_path / "missing-rules.json", allowed_dirs=[tmp_path])
        guard.bind_definitions(registry.definitions())
        llm = _ApprovalLLM(target)
        events = []

        async def consume():
            async for event in react_event_loop(
                llm,
                [{"role": "user", "content": "write"}],
                registry,
                guard,
                max_iters=3,
            ):
                events.append(event)
                if event.type is EventType.TOOL_CALL_RESULT and event.data.get("approval_required"):
                    assert APPROVAL_BROKER.resolve(
                        "run-1", str(event.data["approval_id"]), True
                    )

        with use_approval_context(APPROVAL_BROKER, "run-1"):
            await consume()
        return events, target

    events, target = asyncio.run(run())
    assert target.read_text(encoding="utf-8") == "approved"
    approval_events = [
        event for event in events
        if event.type is EventType.TOOL_CALL_RESULT and event.data.get("approval_required")
    ]
    assert len(approval_events) == 1
    assert approval_events[0].data["arguments"] == {
        "path": str(target),
        "content": "approved",
    }
    assert approval_events[0].data["presentation"] == {
        "title": "Write a file",
        "summary": f"Write to {target}",
        "details": [
            {"label": "Path", "value": str(target)},
            {"label": "Content preview", "value": "approved"},
        ],
        "reason": "This will change file contents.",
    }
    assert any(event.type is EventType.TEXT_DELTA and event.data.get("text") == "done" for event in events)


def test_react_loop_treats_approval_timeout_as_blocked_without_executing_tool(tmp_path: Path) -> None:
    class TimedOutBroker(ApprovalBroker):
        async def wait(self, request, *, timeout_seconds=300.0):
            raise ApprovalTimeoutError("Approval timed out")

    async def run():
        target = tmp_path / "must-not-exist.txt"
        registry = ToolRegistry()

        async def write_file(path: str, content: str):
            Path(path).write_text(content, encoding="utf-8")
            return "written"

        registry.register(
            "write_file", "Write", {}, write_file,
            permission_level="write", approval_policy="policy", side_effect="write",
        )
        guard = ToolGuard(tmp_path / "missing-rules.json", allowed_dirs=[tmp_path])
        guard.bind_definitions(registry.definitions())
        events = []
        with use_approval_context(TimedOutBroker(), "run-1"):
            async for event in react_event_loop(
                _ApprovalLLM(target),
                [{"role": "user", "content": "write"}],
                registry,
                guard,
                max_iters=3,
            ):
                events.append(event)
        return events, target

    events, target = asyncio.run(run())

    assert not target.exists()
    timeout_events = [
        event for event in events
        if event.type is EventType.TOOL_CALL_RESULT and event.data.get("reason") == "Approval timed out"
    ]
    assert len(timeout_events) == 1
    assert timeout_events[0].data["blocked"] is True
    assert timeout_events[0].data["approval_outcome"] == "timed_out"


class _ApprovalLLM:
    def __init__(self, target: Path) -> None:
        self.target = target
        self.calls = 0

    async def chat(self, messages, tools=None, prefix_cache_key=None):
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(
                tool_calls=[
                    ToolCallData(
                        id="tool-1",
                        name="write_file",
                        arguments={"path": str(self.target), "content": "approved"},
                    )
                ]
            )
        return ChatResponse(text="done")
