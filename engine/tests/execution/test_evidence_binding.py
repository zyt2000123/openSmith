"""Evidence binding: gate verdicts are cryptographically tied to tool results.

A gate verdict is only as trustworthy as the evidence it was computed from.
``TOOL_CALL_RESULT`` carries a SHA-256 over the FULL tool result (before the
transport truncation), and a pipeline node's ``GATE_RESULT`` carries an
``evidence_hash`` over the ordered list of those bindings — so a reviewer can
re-derive the hash and confirm the verdict was based on the exact outputs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.execution.evidence import evidence_hash_of, tool_result_hash
from engine.execution.events import EventType, ExecutionEvent
from engine.execution.orchestration.agent_loop import run_agent_stream
from engine.execution.pipeline.backtrack import FailureLoopGuard
from engine.execution.pipeline.gate import GateResult
from engine.execution.pipeline.skill_chain import SkillChain, SkillNode
from engine.execution.react.react_loop import react_event_loop
from engine.identity import IdentitySpec, RouteDecision
from engine.llm.client import ChatResponse
from engine.llm.contracts import ToolCallData
from engine.safety.tool_guard import ToolGuard
from engine.skill.loader import SkillBody, SkillMeta
from engine.tool.registry import ToolRegistry

_SMITH = IdentitySpec(
    id="smith", name="Smith", description="", prompt="",
    enabled_tools=None, enabled_skills=None, routes=(), is_default=True,
)
FEATURE_ROUTE = RouteDecision(_SMITH, "feature", "feature", score=1)


def _echo_registry(tmp_path: Path) -> tuple[ToolRegistry, ToolGuard]:
    registry = ToolRegistry()

    def echo_tool(text: str) -> str:
        return f"echo:{text}"

    registry.register(
        "echo_tool",
        "Echo a string",
        {"properties": {"text": {"type": "string"}}, "required": ["text"]},
        echo_tool,
        permission_level="read",
        approval_policy="never",
        side_effect="none",
    )
    guard = ToolGuard(tmp_path / "rules.json", allowed_dirs=[tmp_path])
    guard.bind_definitions(registry.definitions())
    registry.bind_tool_guard(guard)
    return registry, guard


class _ToolLLM:
    """First call emits tool calls, later calls produce a final reply."""

    def __init__(self, first_calls: list[ToolCallData]) -> None:
        self.first_calls = first_calls
        self.calls = 0

    async def chat(self, messages, tools=None, prefix_cache_key=None):
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(tool_calls=self.first_calls)
        return ChatResponse(text="final answer")


def test_tool_result_hash_is_deterministic_and_content_sensitive() -> None:
    base = dict(
        tool_name="echo_tool",
        call_id="c1",
        arguments={"text": "hi"},
        content="echo:hi",
        is_error=False,
    )
    first = tool_result_hash(**base)
    assert first == tool_result_hash(**base)

    assert first != tool_result_hash(**{**base, "content": "echo:hi!"})
    assert first != tool_result_hash(**{**base, "call_id": "c2"})
    assert first != tool_result_hash(**{**base, "arguments": {"text": "hey"}})
    assert first != tool_result_hash(**{**base, "is_error": True})


def test_evidence_hash_tracks_the_evidence_list() -> None:
    evidence = [
        {"tool": "read_file", "call_id": "a", "result_hash": "x", "error": False},
        {"tool": "grep", "call_id": "b", "result_hash": "y", "error": False},
    ]
    assert evidence_hash_of(evidence) == evidence_hash_of(evidence)
    assert evidence_hash_of(evidence) != evidence_hash_of(evidence[:-1])
    assert evidence_hash_of(evidence) != evidence_hash_of(list(reversed(evidence)))


@pytest.mark.asyncio
async def test_react_loop_binds_executed_tool_result(tmp_path: Path) -> None:
    registry, guard = _echo_registry(tmp_path)
    llm = _ToolLLM([ToolCallData(id="c1", name="echo_tool", arguments={"text": "hello"})])

    events: list[ExecutionEvent] = []
    async for event in react_event_loop(
        llm,
        [{"role": "user", "content": "go"}],
        registry,
        guard,
        max_iters=3,
    ):
        events.append(event)

    executed = [
        event for event in events
        if event.type is EventType.TOOL_CALL_RESULT and event.data.get("result_hash")
    ]
    assert len(executed) == 1
    assert executed[0].data["name"] == "echo_tool"
    assert executed[0].data["result_hash"] == tool_result_hash(
        tool_name="echo_tool",
        call_id="c1",
        arguments={"text": "hello"},
        content="echo:hello",
        is_error=False,
    )


@pytest.mark.asyncio
async def test_react_loop_disabled_tool_has_no_result_binding(tmp_path: Path) -> None:
    """A call that never executed (disabled tool) must not become evidence."""
    registry, guard = _echo_registry(tmp_path)
    llm = _ToolLLM([ToolCallData(id="c9", name="no_such_tool", arguments={})])

    events: list[ExecutionEvent] = []
    async for event in react_event_loop(
        llm,
        [{"role": "user", "content": "go"}],
        registry,
        guard,
        max_iters=3,
    ):
        events.append(event)

    disabled = [
        event for event in events
        if event.type is EventType.TOOL_CALL_RESULT
        and event.data.get("error_kind") == "tool_disabled"
    ]
    assert len(disabled) == 1
    assert "result_hash" not in disabled[0].data


@pytest.mark.asyncio
async def test_pipeline_gate_result_binds_node_evidence(tmp_path: Path) -> None:
    class PassGate:
        async def check(self, output, context):
            return GateResult("pass", "ok")

    class FakeSkillRegistry:
        def get(self, name):
            return SkillBody(meta=SkillMeta(name=name), content="Do the work.")

    registry, guard = _echo_registry(tmp_path)
    chain = SkillChain([SkillNode("planning", PassGate())])
    llm = _ToolLLM([ToolCallData(id="p1", name="echo_tool", arguments={"text": "evidence"})])

    events: list[ExecutionEvent] = []
    async for event in run_agent_stream(
        llm,
        "system prompt",
        "do the work",
        registry,
        FakeSkillRegistry(),
        FEATURE_ROUTE,
        chain,
        FailureLoopGuard(),
        tool_guard=guard,
        execution_context={
            "agent_id": "a",
            "session_id": "sess-ev",
            "_state_dir": str(tmp_path),
        },
    ):
        events.append(event)

    gate_results = [e for e in events if e.type is EventType.GATE_RESULT]
    gate_evidence = [e for e in events if e.type is EventType.GATE_EVIDENCE]
    assert len(gate_results) == 1
    assert len(gate_evidence) == 1

    evidence = gate_evidence[0].data["evidence"]
    assert evidence == [
        {
            "tool": "echo_tool",
            "call_id": "p1",
            "result_hash": tool_result_hash(
                tool_name="echo_tool",
                call_id="p1",
                arguments={"text": "evidence"},
                content="echo:evidence",
                is_error=False,
            ),
            "error": False,
        }
    ]
    expected_hash = evidence_hash_of(evidence)
    assert gate_evidence[0].data["evidence_hash"] == expected_hash
    assert gate_results[0].data["evidence_hash"] == expected_hash
