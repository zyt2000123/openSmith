from __future__ import annotations

import asyncio
from pathlib import Path

from engine.execution.orchestration import lifecycle as lifecycle_module
from engine.execution.orchestration.agent_loop import run_agent_stream
from engine.execution.pipeline.backtrack import FailureLoopGuard
from engine.execution.events import EventType, ExecutionEvent
from engine.execution.pipeline.gate import GateResult, LLMGate
from engine.execution.pipeline.skill_chain import (
    GATE_REGISTRY,
    SkillChain,
    SkillNode,
    load_gate_content,
)
from engine.identity import IdentitySpec, RouteDecision
from engine.llm.client import ChatResponse, ToolCallData
from engine.llm.events import ProviderEvent, ProviderEventType
from engine.skill.loader import SkillBody, SkillMeta


load_gate_content(Path(__file__).resolve().parents[3] / "agents")


def _rubric_gate():
    factory = GATE_REGISTRY["rubric"]
    return factory() if callable(factory) else factory


_SMITH_IDENTITY = IdentitySpec(
    id="smith",
    name="Smith",
    description="",
    prompt="",
    enabled_tools=None,
    enabled_skills=None,
    routes=(),
    is_default=True,
)
FEATURE_ROUTE = RouteDecision(_SMITH_IDENTITY, "feature", "feature", score=1)
DIRECT_ROUTE = RouteDecision(_SMITH_IDENTITY, "direct", None)


class FakeLLM:
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        prefix_cache_key: str | None = None,
    ) -> ChatResponse:
        return ChatResponse(
            text=(
                "Completed the requested work with evidence in "
                "engine/execution/orchestration/agent_loop.py and enough detail for review."
            )
        )


class StreamingFakeLLM(FakeLLM):
    stream = True

    async def chat_events(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ):
        text = (
            "Completed the requested work with evidence in "
            "engine/execution/orchestration/agent_loop.py and enough detail for review."
        )
        yield ProviderEvent(ProviderEventType.RESPONSE_CREATED)
        yield ProviderEvent(ProviderEventType.OUTPUT_TEXT_DELTA, {"delta": text})
        yield ProviderEvent(
            ProviderEventType.RESPONSE_COMPLETED,
            {"finish_reason": "stop", "raw_finish_reason": "stop"},
        )


class BrokenStreamingFakeLLM(FakeLLM):
    stream = True

    async def chat_events(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ):
        yield ProviderEvent(ProviderEventType.RESPONSE_CREATED)
        yield ProviderEvent(ProviderEventType.OUTPUT_TEXT_DELTA, {"delta": "unfinished draft"})
        raise RuntimeError("provider disconnected")


class ContentFilteredLLM(FakeLLM):
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        prefix_cache_key: str | None = None,
    ) -> ChatResponse:
        return ChatResponse(
            text="UNGATED CONTENT-FILTERED DRAFT",
            finish_reason="content_filter",
        )


class FailedToolCallLLM(FakeLLM):
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        prefix_cache_key: str | None = None,
    ) -> ChatResponse:
        return ChatResponse(
            text="UNGATED FAILED TOOL-CALL DRAFT",
            tool_calls=[ToolCallData(id="call-1", name="unused", arguments={})],
            finish_reason="error",
        )


class RetryingStreamingFakeLLM(FakeLLM):
    stream = True

    def __init__(self) -> None:
        self.outputs = [
            "first draft",
            "second draft",
            "Detailed accepted output with evidence from engine/example.py and verification details.",
        ]

    async def chat_events(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ):
        yield ProviderEvent(ProviderEventType.RESPONSE_CREATED)
        yield ProviderEvent(ProviderEventType.OUTPUT_TEXT_DELTA, {"delta": self.outputs.pop(0)})
        yield ProviderEvent(
            ProviderEventType.RESPONSE_COMPLETED,
            {"finish_reason": "stop", "raw_finish_reason": "stop"},
        )


class FakeToolRegistry:
    def get_schemas(self) -> list[dict]:
        return []


class FakeSkillRegistry:
    def get(self, name: str) -> SkillBody:
        return SkillBody(meta=SkillMeta(name=name), content="Do the work.")


class PassingGate:
    async def check(self, output: str, context: dict) -> GateResult:
        return GateResult("pass", "ok")


class RecordingLLMGate(LLMGate):
    def __init__(self) -> None:
        self.selected_llm = None

    def set_llm(self, llm) -> None:
        self.selected_llm = llm

    async def check(self, output: str, context: dict) -> GateResult:
        return GateResult("pass", "ok")


def test_run_agent_stream_saves_and_clears_checkpoint(tmp_path: Path) -> None:
    async def run() -> bool:
        state_path = tmp_path / "sessions" / ".state" / "sess-1.json"
        saw_checkpoint = False

        async for event in run_agent_stream(
            FakeLLM(),
            "system prompt",
            "build a feature",
            FakeToolRegistry(),
            FakeSkillRegistry(),
            FEATURE_ROUTE,
            SkillChain([SkillNode("planning", PassingGate())]),
            FailureLoopGuard(),
            execution_context={
                "agent_id": "smith-id",
                "session_id": "sess-1",
                "_state_dir": str(tmp_path),
            },
        ):
            if event.type == EventType.SKILL_END:
                saw_checkpoint = state_path.is_file()

        assert not state_path.exists()
        return saw_checkpoint

    assert asyncio.run(run())


def test_run_agent_stream_forwards_provider_events_from_skill_nodes() -> None:
    async def run():
        events = []
        async for event in run_agent_stream(
            StreamingFakeLLM(),
            "system prompt",
            "build a feature",
            FakeToolRegistry(),
            FakeSkillRegistry(),
            FEATURE_ROUTE,
            SkillChain([SkillNode("planning", PassingGate())]),
            FailureLoopGuard(),
        ):
            events.append(event)
        return events

    events = asyncio.run(run())

    provisional = next(event for event in events if event.type == EventType.PROVISIONAL_TEXT_DELTA)
    assert len(provisional.data["provision_id"].rsplit(":", 1)[-1]) == 32
    assert any(event.type == EventType.PROVISIONAL_COMMIT for event in events)
    final = [event for event in events if event.type == EventType.TEXT_DELTA]
    assert final == [
        ExecutionEvent(EventType.TEXT_DELTA, {
            "text": "Completed the requested work with evidence in "
            "engine/execution/orchestration/agent_loop.py and enough detail for review.",
            "already_streamed": True,
        })
    ]


def test_run_agent_stream_routes_llm_gates_to_gate_client() -> None:
    async def run() -> RecordingLLMGate:
        gate = RecordingLLMGate()
        primary_llm = FakeLLM()
        gate_llm = FakeLLM()
        async for _ in run_agent_stream(
            primary_llm,
            "system prompt",
            "build a feature",
            FakeToolRegistry(),
            FakeSkillRegistry(),
            FEATURE_ROUTE,
            SkillChain([SkillNode("planning", gate)]),
            FailureLoopGuard(),
            gate_llm=gate_llm,
        ):
            pass
        assert gate.selected_llm is gate_llm
        return gate

    asyncio.run(run())


def test_forced_skill_marks_final_text_already_streamed() -> None:
    async def run():
        events = []
        async for event in run_agent_stream(
            StreamingFakeLLM(),
            "system prompt",
            "run planning",
            FakeToolRegistry(),
            FakeSkillRegistry(),
            DIRECT_ROUTE,
            None,
            FailureLoopGuard(),
            forced_skill="planning",
        ):
            events.append(event)
        return events

    events = asyncio.run(run())
    final = [event for event in events if event.type == EventType.TEXT_DELTA]

    assert final == [
        ExecutionEvent(EventType.TEXT_DELTA, {
            "text": "Completed the requested work with evidence in "
            "engine/execution/orchestration/agent_loop.py and enough detail for review.",
            "already_streamed": True,
        })
    ]


def test_text_stream_adapter_skips_provisional_text() -> None:
    async def fake_reply_events(*args):
        yield ExecutionEvent(EventType.PROVISIONAL_TEXT_DELTA, {
            "provision_id": "planning:0:0:abc",
            "text": "provisional draft",
        })
        yield ExecutionEvent(EventType.PROVISIONAL_COMMIT, {"provision_id": "planning:0:0:abc"})
        yield ExecutionEvent(EventType.TEXT_DELTA, {"text": "final answer", "already_streamed": True})

    async def run() -> list[str]:
        original = lifecycle_module.reply_events_with_runtime
        lifecycle_module.reply_events_with_runtime = fake_reply_events
        try:
            return [
                chunk
                async for chunk in lifecycle_module.reply_stream_with_runtime(None, None, None)
            ]
        finally:
            lifecycle_module.reply_events_with_runtime = original

    assert asyncio.run(run()) == ["final answer"]


def test_pipeline_retracts_provisional_draft_before_propagating_provider_error() -> None:
    async def run():
        events = []
        try:
            async for event in run_agent_stream(
                BrokenStreamingFakeLLM(),
                "system prompt",
                "build a feature",
                FakeToolRegistry(),
                FakeSkillRegistry(),
                FEATURE_ROUTE,
                SkillChain([SkillNode("planning", PassingGate())]),
                FailureLoopGuard(),
            ):
                events.append(event)
        except RuntimeError as exc:
            assert str(exc) == "provider disconnected"
        return events

    events = asyncio.run(run())
    provisional = next(event for event in events if event.type == EventType.PROVISIONAL_TEXT_DELTA)
    retraction = next(event for event in events if event.type == EventType.PROVISIONAL_RETRACT)

    assert retraction.data == {
        "provision_id": provisional.data["provision_id"],
        "reason": "execution_error",
    }


def test_pipeline_never_emits_ungated_text_after_an_incomplete_or_failed_node() -> None:
    """A terminal node failure must not turn its provisional draft into a reply."""

    async def collect(llm) -> list[ExecutionEvent]:
        events: list[ExecutionEvent] = []
        async for event in run_agent_stream(
            llm,
            "system prompt",
            "build a feature",
            FakeToolRegistry(),
            FakeSkillRegistry(),
            FEATURE_ROUTE,
            SkillChain([SkillNode("planning", PassingGate())]),
            FailureLoopGuard(),
        ):
            events.append(event)
        return events

    incomplete_events = asyncio.run(collect(ContentFilteredLLM()))
    failed_events = asyncio.run(collect(FailedToolCallLLM()))

    assert EventType.INCOMPLETE in [event.type for event in incomplete_events]
    assert EventType.FAILED in [event.type for event in failed_events]
    assert not [event for event in incomplete_events if event.type is EventType.TEXT_DELTA]
    assert not [event for event in failed_events if event.type is EventType.TEXT_DELTA]


def test_pipeline_retracts_each_rejected_rubric_attempt_before_committing() -> None:
    async def run():
        events = []
        async for event in run_agent_stream(
            RetryingStreamingFakeLLM(),
            "system prompt",
            "build a feature",
            FakeToolRegistry(),
            FakeSkillRegistry(),
            FEATURE_ROUTE,
            # 兜底层由 YAML/调用方声明；这里显式挂 rubric 复现原有重试行为
            SkillChain([SkillNode("planning", PassingGate())], base_gates=[_rubric_gate()]),
            FailureLoopGuard(),
        ):
            events.append(event)
        return events

    events = asyncio.run(run())
    provision_ids = [
        event.data["provision_id"]
        for event in events
        if event.type == EventType.PROVISIONAL_TEXT_DELTA
    ]
    retracted_ids = [
        event.data["provision_id"]
        for event in events
        if event.type == EventType.PROVISIONAL_RETRACT
    ]
    committed_ids = [
        event.data["provision_id"]
        for event in events
        if event.type == EventType.PROVISIONAL_COMMIT
    ]

    assert len(provision_ids) == 3
    assert retracted_ids == provision_ids[:2]
    assert committed_ids == provision_ids[2:]


def test_base_gate_runs_before_node_gate() -> None:
    """两层门禁顺序：兜底层(base_gates) → 领域层(节点 gate)。"""
    calls: list[str] = []

    class RecordingBaseGate:
        async def check(self, output: str, context: dict) -> GateResult:
            calls.append("base")
            return GateResult("pass", "ok")

    class RecordingNodeGate:
        async def check(self, output: str, context: dict) -> GateResult:
            calls.append("node")
            return GateResult("pass", "ok")

    async def run():
        async for _ in run_agent_stream(
            FakeLLM(),
            "system prompt",
            "build a feature",
            FakeToolRegistry(),
            FakeSkillRegistry(),
            FEATURE_ROUTE,
            SkillChain(
                [SkillNode("planning", RecordingNodeGate())],
                base_gates=[RecordingBaseGate()],
            ),
            FailureLoopGuard(),
        ):
            pass

    asyncio.run(run())
    assert calls == ["base", "node"]


def test_empty_base_gates_skip_straight_to_node_gate() -> None:
    """未声明兜底层时不做任何兜底检查，单次执行直接进领域门禁。"""
    calls: list[str] = []

    class RecordingNodeGate:
        async def check(self, output: str, context: dict) -> GateResult:
            calls.append("node")
            return GateResult("pass", "ok")

    async def run():
        async for _ in run_agent_stream(
            FakeLLM(),
            "system prompt",
            "build a feature",
            FakeToolRegistry(),
            FakeSkillRegistry(),
            FEATURE_ROUTE,
            SkillChain([SkillNode("planning", RecordingNodeGate())]),
            FailureLoopGuard(),
        ):
            pass

    asyncio.run(run())
    assert calls == ["node"]


class PassThenBrokenStreamingLLM(FakeLLM):
    """第一次调用正常完成，第二次在产出内容后断流。"""

    stream = True

    def __init__(self) -> None:
        self.calls = 0

    async def chat_events(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ):
        self.calls += 1
        yield ProviderEvent(ProviderEventType.RESPONSE_CREATED)
        yield ProviderEvent(ProviderEventType.OUTPUT_TEXT_DELTA, {"delta": "node output "})
        if self.calls == 1:
            yield ProviderEvent(
                ProviderEventType.RESPONSE_COMPLETED,
                {"finish_reason": "stop", "raw_finish_reason": "stop"},
            )
        else:
            raise RuntimeError("provider disconnected")


def test_execution_error_clears_saved_checkpoint(tmp_path: Path) -> None:
    """节点间执行异常不能留下孤儿 checkpoint（restore 未接线，无人消费）。"""

    async def run() -> tuple[bool, bool]:
        state_path = tmp_path / "sessions" / ".state" / "sess-err.json"
        saw_checkpoint = False
        try:
            async for event in run_agent_stream(
                PassThenBrokenStreamingLLM(),
                "system prompt",
                "build a feature",
                FakeToolRegistry(),
                FakeSkillRegistry(),
                FEATURE_ROUTE,
                SkillChain([
                    SkillNode("planning", PassingGate()),
                    SkillNode("testing", PassingGate()),
                ]),
                FailureLoopGuard(),
                execution_context={
                    "agent_id": "smith-id",
                    "session_id": "sess-err",
                    "_state_dir": str(tmp_path),
                },
            ):
                if event.type == EventType.SKILL_END:
                    saw_checkpoint = saw_checkpoint or state_path.is_file()
        except RuntimeError as exc:
            assert str(exc) == "provider disconnected"
        return saw_checkpoint, state_path.exists()

    saw_checkpoint, still_exists = asyncio.run(run())
    assert saw_checkpoint
    assert not still_exists


class RecordingStreamingLLM(FakeLLM):
    stream = True

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def chat_events(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ):
        self.calls.append([dict(m) for m in messages])
        yield ProviderEvent(ProviderEventType.RESPONSE_CREATED)
        yield ProviderEvent(ProviderEventType.OUTPUT_TEXT_DELTA, {"delta": "candidate output"})
        yield ProviderEvent(
            ProviderEventType.RESPONSE_COMPLETED,
            {"finish_reason": "stop", "raw_finish_reason": "stop"},
        )


def _seed_checkpoint(
    tmp_path: Path,
    session_id: str,
    user_message: str,
    *,
    working_dir: Path | None = None,
    agent_id: str = "smith-id",
    identity_id: str = "smith",
) -> None:
    from engine.execution.pipeline.checkpoint import SessionCheckpoint, SessionStateManager

    SessionStateManager(tmp_path).save(SessionCheckpoint(
        agent_id=agent_id,
        session_id=session_id,
        identity_id=identity_id,
        route_id="feature",
        skill_chain_index=0,  # planning already passed before the crash
        context={
            "user_message": user_message,
            "identity_id": identity_id,
            "route_id": "feature",
            "agent_id": agent_id,
            "session_id": session_id,
            "planning_output": "PLAN-FROM-CHECKPOINT",
        },
        timestamp="2026-07-15T00:00:00+00:00",
        working_dir=str((working_dir or tmp_path).resolve()),
    ))


def test_run_agent_stream_resumes_from_crash_checkpoint(tmp_path: Path) -> None:
    """崩溃遗留的 checkpoint（同 route + 同消息）恢复时跳过已完成节点。"""
    _seed_checkpoint(tmp_path, "sess-resume", "build a feature")

    async def run():
        llm = RecordingStreamingLLM()
        events = []
        async for event in run_agent_stream(
            llm,
            "system prompt",
            "build a feature",
            FakeToolRegistry(),
            FakeSkillRegistry(),
            FEATURE_ROUTE,
            SkillChain([
                SkillNode("planning", PassingGate()),
                SkillNode("testing", PassingGate()),
            ]),
            FailureLoopGuard(),
            execution_context={
                "agent_id": "smith-id",
                "session_id": "sess-resume",
                "_state_dir": str(tmp_path),
                "_working_dir": str(tmp_path.resolve()),
            },
        ):
            events.append(event)
        return llm, events

    llm, events = asyncio.run(run())
    started = [e.data["skill"] for e in events if e.type == EventType.SKILL_START]
    assert started == ["testing"], "planning must be skipped, not re-run"
    assert len(llm.calls) == 1
    assert not (tmp_path / "sessions" / ".state" / "sess-resume.json").exists()


def test_run_agent_stream_discards_stale_checkpoint_for_new_task(tmp_path: Path) -> None:
    """消息不同 → 陈旧 checkpoint 清除，链从头执行，不残留可恢复状态。"""
    _seed_checkpoint(tmp_path, "sess-stale", "build a feature")

    async def run():
        llm = RecordingStreamingLLM()
        events = []
        async for event in run_agent_stream(
            llm,
            "system prompt",
            "a completely different task",
            FakeToolRegistry(),
            FakeSkillRegistry(),
            FEATURE_ROUTE,
            SkillChain([
                SkillNode("planning", PassingGate()),
                SkillNode("testing", PassingGate()),
            ]),
            FailureLoopGuard(),
            execution_context={
                "agent_id": "smith-id",
                "session_id": "sess-stale",
                "_state_dir": str(tmp_path),
                "_working_dir": str(tmp_path.resolve()),
            },
        ):
            events.append(event)
        return llm, events

    llm, events = asyncio.run(run())
    started = [e.data["skill"] for e in events if e.type == EventType.SKILL_START]
    assert started == ["planning", "testing"]
    assert len(llm.calls) == 2
    assert not (tmp_path / "sessions" / ".state" / "sess-stale.json").exists()


def test_run_agent_stream_discards_checkpoint_from_another_working_directory(tmp_path: Path) -> None:
    """A reused session must not carry plans from project A into project B."""
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    _seed_checkpoint(tmp_path, "sess-workspace", "build a feature", working_dir=workspace_a)

    async def run():
        llm = RecordingStreamingLLM()
        events = []
        async for event in run_agent_stream(
            llm,
            "system prompt",
            "build a feature",
            FakeToolRegistry(),
            FakeSkillRegistry(),
            FEATURE_ROUTE,
            SkillChain([
                SkillNode("planning", PassingGate()),
                SkillNode("testing", PassingGate()),
            ]),
            FailureLoopGuard(),
            execution_context={
                "agent_id": "smith-id",
                "session_id": "sess-workspace",
                "_state_dir": str(tmp_path),
                "_working_dir": str(workspace_b.resolve()),
            },
        ):
            events.append(event)
        return llm, events

    llm, events = asyncio.run(run())
    started = [event.data["skill"] for event in events if event.type is EventType.SKILL_START]

    assert started == ["planning", "testing"]
    assert len(llm.calls) == 2
    assert not (tmp_path / "sessions" / ".state" / "sess-workspace.json").exists()


def test_run_agent_stream_discards_checkpoint_from_another_agent(tmp_path: Path) -> None:
    _seed_checkpoint(tmp_path, "sess-agent", "build a feature")

    async def run():
        llm = RecordingStreamingLLM()
        events = []
        async for event in run_agent_stream(
            llm,
            "system prompt",
            "build a feature",
            FakeToolRegistry(),
            FakeSkillRegistry(),
            FEATURE_ROUTE,
            SkillChain([
                SkillNode("planning", PassingGate()),
                SkillNode("testing", PassingGate()),
            ]),
            FailureLoopGuard(),
            execution_context={
                "agent_id": "another-agent",
                "session_id": "sess-agent",
                "_state_dir": str(tmp_path),
                "_working_dir": str(tmp_path.resolve()),
            },
        ):
            events.append(event)
        return llm, events

    llm, events = asyncio.run(run())
    started = [event.data["skill"] for event in events if event.type is EventType.SKILL_START]

    assert started == ["planning", "testing"]
    assert len(llm.calls) == 2
    assert not (tmp_path / "sessions" / ".state" / "sess-agent.json").exists()


def test_run_agent_stream_discards_checkpoint_from_another_identity(tmp_path: Path) -> None:
    _seed_checkpoint(tmp_path, "sess-identity", "build a feature")
    other_identity = IdentitySpec(
        id="other",
        name="Other",
        description="",
        prompt="",
        enabled_tools=None,
        enabled_skills=None,
        routes=(),
        is_default=False,
    )
    other_route = RouteDecision(other_identity, "feature", "feature", score=1)

    async def run():
        llm = RecordingStreamingLLM()
        events = []
        async for event in run_agent_stream(
            llm,
            "system prompt",
            "build a feature",
            FakeToolRegistry(),
            FakeSkillRegistry(),
            other_route,
            SkillChain([
                SkillNode("planning", PassingGate()),
                SkillNode("testing", PassingGate()),
            ]),
            FailureLoopGuard(),
            execution_context={
                "agent_id": "smith-id",
                "session_id": "sess-identity",
                "_state_dir": str(tmp_path),
                "_working_dir": str(tmp_path.resolve()),
            },
        ):
            events.append(event)
        return llm, events

    llm, events = asyncio.run(run())
    started = [event.data["skill"] for event in events if event.type is EventType.SKILL_START]

    assert started == ["planning", "testing"]
    assert len(llm.calls) == 2
    assert not (tmp_path / "sessions" / ".state" / "sess-identity.json").exists()


def test_domain_gate_retry_hint_reaches_retry_attempt() -> None:
    """域门禁 retry_hint 必须随重试流回节点，而不是被静默丢弃。"""

    class RetryHintGate:
        def __init__(self) -> None:
            self.checks = 0

        async def check(self, output: str, context: dict) -> GateResult:
            self.checks += 1
            if self.checks == 1:
                return GateResult("fail", "too vague", retry_hint="ADD-EVIDENCE-HINT")
            return GateResult("pass", "ok")

    async def run() -> tuple[RecordingStreamingLLM, list[ExecutionEvent]]:
        llm = RecordingStreamingLLM()
        events: list[ExecutionEvent] = []
        async for event in run_agent_stream(
            llm,
            "system prompt",
            "build a feature",
            FakeToolRegistry(),
            FakeSkillRegistry(),
            FEATURE_ROUTE,
            SkillChain([SkillNode("planning", RetryHintGate())]),
            FailureLoopGuard(),
        ):
            events.append(event)
        return llm, events

    llm, events = asyncio.run(run())
    assert len(llm.calls) == 2
    first = "".join(str(m.get("content", "")) for m in llm.calls[0])
    second = "".join(str(m.get("content", "")) for m in llm.calls[1])
    assert "ADD-EVIDENCE-HINT" not in first
    assert "ADD-EVIDENCE-HINT" in second
    assert [event.data["status"] for event in events if event.type is EventType.SKILL_END] == ["retry", "passed"]


def test_pipeline_closes_the_started_skill_when_a_base_gate_blocks() -> None:
    class RejectingBaseGate:
        async def check(self, output: str, context: dict) -> GateResult:
            return GateResult("retry", "missing required evidence")

    async def run() -> list[ExecutionEvent]:
        events: list[ExecutionEvent] = []
        async for event in run_agent_stream(
            RetryingStreamingFakeLLM(),
            "system prompt",
            "build a feature",
            FakeToolRegistry(),
            FakeSkillRegistry(),
            FEATURE_ROUTE,
            SkillChain([SkillNode("planning", PassingGate())], base_gates=[RejectingBaseGate()]),
            FailureLoopGuard(),
        ):
            events.append(event)
        return events

    events = asyncio.run(run())
    assert [(event.type, event.data.get("status")) for event in events if event.type is EventType.SKILL_END] == [
        (EventType.SKILL_END, "blocked"),
    ]
