"""Pipeline skills must retain the assembled runtime prompt and bounded handoff."""

from __future__ import annotations

import asyncio

from engine.execution.events import EventType
from engine.execution.orchestration.agent_loop import run_agent_stream
from engine.execution.pipeline.backtrack import FailureLoopGuard
from engine.execution.pipeline.gate import GateResult
from engine.execution.pipeline.skill_chain import SkillChain, SkillNode
from engine.identity import IdentitySpec, RouteDecision
from engine.llm.client import ChatResponse
from engine.skill.loader import SkillBody, SkillMeta

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
