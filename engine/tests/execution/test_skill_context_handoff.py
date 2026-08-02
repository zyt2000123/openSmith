"""Pipeline skills must retain the assembled runtime prompt and bounded handoff."""

from __future__ import annotations

import asyncio
from pathlib import Path

from engine.execution.events import EventType
from engine.execution.orchestration.agent_loop import run_agent_stream
from engine.execution.pipeline.backtrack import FailureLoopGuard
from engine.execution.pipeline.gate import GateResult
from engine.execution.pipeline.skill_chain import SkillChain, SkillNode
from engine.identity import IdentitySpec, RouteDecision
from engine.llm.client import ChatResponse, ToolCallData
from engine.llm.events import ProviderEvent, ProviderEventType
from engine.safety.tool_guard import ToolGuard
from engine.skill.loader import SkillBody, SkillMeta
from engine.tool.registry import ToolRegistry

_IDENTITY = IdentitySpec(
    id="smith",
    name="Smith",
    description="",
    prompt="",
    enabled_tools=None,
    enabled_skills=None,
    routes=(),
    is_default=True,
)
_FEATURE_ROUTE = RouteDecision(_IDENTITY, "feature", "feature", score=1)
_DIRECT_ROUTE = RouteDecision(_IDENTITY, "direct", None, score=1)


class RecordingLLM:
    def __init__(self, responses: list[str]) -> None:
        self._responses = iter(responses)
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools=None, prefix_cache_key=None):
        self.calls.append([dict(message) for message in messages])
        return ChatResponse(text=next(self._responses))


class FakeToolRegistry:
    def get_schemas(self):
        return []


class TwoSkillRegistry:
    def get(self, name: str) -> SkillBody | None:
        content = {
            "plan": "Plan the requested implementation.",
            "implement": "Implement the plan using the available evidence.",
        }.get(name)
        return None if content is None else SkillBody(SkillMeta(name=name), content)


class PassingGate:
    async def check(self, output: str, context: dict) -> GateResult:
        return GateResult("pass", "accepted")


def test_pipeline_node_exposes_only_its_declared_tool_scope() -> None:
    class ToolRecordingLLM:
        def __init__(self) -> None:
            self.tool_names: list[list[str]] = []

        async def chat(self, messages, tools=None, prefix_cache_key=None):
            self.tool_names.append([
                tool["function"]["name"]
                for tool in (tools or [])
            ])
            return ChatResponse(text="plan complete")

    registry = ToolRegistry()
    registry.register("read_file", "", {}, lambda: "READ")
    registry.register("write_file", "", {}, lambda: "WRITE")
    llm = ToolRecordingLLM()
    chain = SkillChain([
        SkillNode("plan", PassingGate(), allowed_tools=("read_file",)),
    ])

    async def run():
        return [
            event
            async for event in run_agent_stream(
                llm,
                "IDENTITY=smith",
                "Create a plan.",
                registry,
                TwoSkillRegistry(),
                _FEATURE_ROUTE,
                chain,
                FailureLoopGuard(),
            )
        ]

    events = asyncio.run(run())

    assert events[-1].type is EventType.DONE
    assert llm.tool_names == [["read_file"]]


def test_pipeline_node_rejects_a_hallucinated_tool_outside_its_scope(
    tmp_path: Path,
) -> None:
    writes: list[str] = []

    class ToolCallingLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, tools=None, prefix_cache_key=None):
            self.calls += 1
            if self.calls == 1:
                return ChatResponse(tool_calls=[
                    ToolCallData(id="write-1", name="write_file", arguments={}),
                ])
            return ChatResponse(text="plan complete")

    registry = ToolRegistry()
    registry.register("read_file", "", {}, lambda: "READ")
    registry.register(
        "write_file",
        "",
        {},
        lambda: writes.append("write") or "WRITE",
        permission_level="write",
        approval_policy="always",
        side_effect="write",
    )
    tool_guard = ToolGuard(
        tmp_path / "dangerous-commands.json",
        tool_registry=registry.definitions(),
    )
    chain = SkillChain([
        SkillNode("plan", PassingGate(), allowed_tools=("read_file",)),
    ])

    async def run():
        return [
            event
            async for event in run_agent_stream(
                ToolCallingLLM(),
                "IDENTITY=smith",
                "Create a plan.",
                registry,
                TwoSkillRegistry(),
                _FEATURE_ROUTE,
                chain,
                FailureLoopGuard(),
                tool_guard=tool_guard,
            )
        ]

    events = asyncio.run(run())

    result = next(event for event in events if event.type is EventType.TOOL_CALL_RESULT)
    assert result.data["error"] is True
    assert result.data["blocked"] is False
    assert result.data["preflight"] is False
    assert "pipeline node" in result.data["content"]
    assert writes == []


def test_pipeline_skills_keep_assembled_context_and_bound_prior_output() -> None:
    predecessor_output = "BEGIN-PLAN\n" + ("x" * 16_000) + "\nEND-PLAN"
    llm = RecordingLLM([predecessor_output, "implementation complete"])
    chain = SkillChain(
        [
            SkillNode("plan", PassingGate()),
            SkillNode("implement", PassingGate()),
        ]
    )

    async def run():
        return [
            event
            async for event in run_agent_stream(
                llm,
                "IDENTITY=smith\nMEMORY=remember-this\nTOOL_POLICY=read-only-first",
                "Create an implementation plan and then implement it.",
                FakeToolRegistry(),
                TwoSkillRegistry(),
                _FEATURE_ROUTE,
                chain,
                FailureLoopGuard(),
            )
        ]

    events = asyncio.run(run())

    assert events[-1].type is EventType.DONE
    assert len(llm.calls) == 2

    second_system_text = "\n".join(
        str(message["content"])
        for message in llm.calls[1]
        if message.get("role") == "system"
    )
    assert "IDENTITY=smith" in second_system_text
    assert "MEMORY=remember-this" in second_system_text
    assert "TOOL_POLICY=read-only-first" in second_system_text
    assert "Implement the plan using the available evidence." in second_system_text
    assert "_state_dir" not in second_system_text

    handoff = next(
        str(message["content"])
        for message in llm.calls[1]
        if "Prior Workflow Outputs" in str(message.get("content", ""))
    )
    assert "BEGIN-PLAN" in handoff
    assert "END-PLAN" in handoff
    assert "[... truncated ...]" in handoff
    assert len(handoff) < len(predecessor_output)


def test_forced_skill_emits_accumulated_text_before_the_terminal_event() -> None:
    """A consumer that stops at FAILED/INCOMPLETE must still see the output."""

    class LengthStreamingLLM:
        stream = True

        def __init__(self) -> None:
            self.calls = 0

        async def chat_events(self, messages, tools=None):
            self.calls += 1
            yield ProviderEvent(ProviderEventType.RESPONSE_CREATED)
            yield ProviderEvent(
                ProviderEventType.OUTPUT_TEXT_DELTA, {"delta": f"draft-{self.calls}"}
            )
            yield ProviderEvent(
                ProviderEventType.RESPONSE_COMPLETED,
                {"finish_reason": "length", "raw_finish_reason": "length"},
            )

    async def run():
        return [
            event
            async for event in run_agent_stream(
                LengthStreamingLLM(),
                "IDENTITY=smith",
                "plan the work",
                FakeToolRegistry(),
                TwoSkillRegistry(),
                _DIRECT_ROUTE,
                None,
                FailureLoopGuard(),
                forced_skill="plan",
            )
        ]

    events = asyncio.run(run())

    terminal_index = next(
        index
        for index, event in enumerate(events)
        if event.type in {EventType.INCOMPLETE, EventType.FAILED}
    )
    delta_indices = [
        index
        for index, event in enumerate(events)
        if event.type is EventType.TEXT_DELTA
    ]
    assert delta_indices, "accumulated forced-skill text must be emitted"
    assert all(index < terminal_index for index in delta_indices)
    assert "draft-1draft-2draft-3" in events[delta_indices[0]].data["text"]
    assert events[-1].type is EventType.DONE
