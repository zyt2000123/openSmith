"""Unit tests for Module 2: Mid-turn Steering (engine layer)."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.context.compression import (
    STEERING_USER_NOTE_PREFIX,
    active_turn_bounds,
    is_request_turn,
)
from engine.execution.events import EventType
from engine.execution.react.react_loop import react_event_loop
from engine.execution.steering import (
    MAX_STEERING_CONTINUATIONS,
    SteeringBroker,
    SteeringInbox,
    current_steering_inbox,
    use_steering_inbox,
    without_steering_inbox,
)
from engine.llm.contracts import ChatResponse, ToolCallData
from engine.tool.registry import ToolRegistry


class _ScriptedLLM:
    def __init__(self, responses: list[ChatResponse], *, on_call=None) -> None:
        self._responses = list(responses)
        self._on_call = on_call
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools=None, **kwargs) -> ChatResponse:
        idx = len(self.calls)
        self.calls.append([dict(m) for m in messages])
        if self._on_call is not None:
            self._on_call(idx, messages)
        return self._responses.pop(0)


def test_steering_broker_lifecycle_and_limits():
    broker = SteeringBroker()

    # No active run
    res = broker.submit(session_id="s1", content="change direction")
    assert res.accepted is False
    assert res.status == "no_active_run"

    inbox = broker.open_run("run-1", "s1")
    assert broker.active_run_id("s1") == "run-1"

    # Run mismatch
    mismatch = broker.submit(session_id="s1", content="x", run_id="run-old")
    assert mismatch.accepted is False
    assert mismatch.status == "run_mismatch"
    assert mismatch.run_id == "run-1"

    # Normal injection
    ok = broker.submit(session_id="s1", content="steer 1", run_id="run-1")
    assert ok.accepted is True
    assert ok.status == "injected"
    assert ok.steer_id is not None
    assert ok.pending_count == 1

    # Waiting approval status
    wait = broker.submit(
        session_id="s1",
        content="steer 2",
        run_id="run-1",
        waiting_approval=True,
    )
    assert wait.accepted is True
    assert wait.status == "queued_while_waiting_approval"
    assert wait.pending_count == 2

    # Fill to cap
    for i in range(6):
        r = broker.submit(session_id="s1", content=f"extra {i}")
        assert r.accepted is True
    full = broker.submit(session_id="s1", content="overflow")
    assert full.accepted is False
    assert full.status == "inbox_full"

    # Close returns undrained remainder
    undrained = broker.close_run("run-1")
    assert len(undrained) == 8
    assert broker.active_run_id("s1") is None
    after_close = broker.submit(session_id="s1", content="late")
    assert after_close.accepted is False
    assert after_close.status == "no_active_run"
    assert inbox.has_pending() is False


def test_compression_is_request_turn_ignores_steering_prefix():
    steering_msg = {"role": "user", "content": f"{STEERING_USER_NOTE_PREFIX}1. stay focused"}
    normal_msg = {"role": "user", "content": "fix bug"}
    assert is_request_turn(steering_msg) is False
    assert is_request_turn(normal_msg) is True

    convo = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "original task"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
        steering_msg,
    ]
    leading, active_idx = active_turn_bounds(convo)
    assert leading == 1
    assert active_idx == 1  # original task stays protected as active turn


@pytest.mark.asyncio
async def test_steering_folded_before_next_llm_call():
    """Two steering messages pushed during tool execution fold into one user note before next LLM call."""
    registry = ToolRegistry()
    inbox = SteeringInbox()

    async def exec_tool(**_kwargs) -> str:
        inbox.push("Don't touch tests")
        inbox.push("Also check timeout default")
        return "tool output"

    registry.register(
        "inspect",
        "inspect",
        {"type": "object", "properties": {}},
        exec_tool,
        permission_level="read",
        side_effect="none",
        concurrency="safe",
    )

    llm = _ScriptedLLM([
        ChatResponse(
            text="",
            tool_calls=[ToolCallData(id="tc-1", name="inspect", arguments={})],
        ),
        ChatResponse(text="adjusted answer", tool_calls=[]),
    ])

    with use_steering_inbox(inbox):
        events = [
            ev
            async for ev in react_event_loop(
                llm,
                [{"role": "user", "content": "fix the bug"}],
                registry,
            )
        ]

    applied = [ev for ev in events if ev.type is EventType.STEERING_APPLIED]
    assert len(applied) == 1
    assert applied[0].data["count"] == 2
    assert len(applied[0].data["steer_ids"]) == 2
    assert not inbox.has_pending()

    # Check the second LLM call received the single folded steering user message
    second_msgs = llm.calls[1]
    steering_msgs = [
        m for m in second_msgs
        if m.get("role") == "user" and str(m.get("content", "")).startswith(STEERING_USER_NOTE_PREFIX)
    ]
    assert len(steering_msgs) == 1
    assert "1. Don't touch tests" in steering_msgs[0]["content"]
    assert "2. Also check timeout default" in steering_msgs[0]["content"]


@pytest.mark.asyncio
async def test_steering_during_final_answer_triggers_bounded_continuation():
    """Drain point B catches steering that arrives during final answer generation up to MAX_STEERING_CONTINUATIONS."""
    registry = ToolRegistry()
    inbox = SteeringInbox()
    push_count = 0

    def on_call(idx: int, _messages):
        nonlocal push_count
        # Keep pushing one steer during every LLM call to verify MAX_STEERING_CONTINUATIONS cap
        if push_count < MAX_STEERING_CONTINUATIONS + 2:
            inbox.push(f"redirect #{push_count + 1}")
            push_count += 1

    llm = _ScriptedLLM(
        [ChatResponse(text=f"draft {i}", tool_calls=[]) for i in range(MAX_STEERING_CONTINUATIONS + 2)],
        on_call=on_call,
    )

    with use_steering_inbox(inbox):
        events = [
            ev
            async for ev in react_event_loop(
                llm,
                [{"role": "user", "content": "answer me"}],
                registry,
            )
        ]

    applied = [ev for ev in events if ev.type is EventType.STEERING_APPLIED]
    assert len(applied) == MAX_STEERING_CONTINUATIONS
    assert len(llm.calls) == MAX_STEERING_CONTINUATIONS + 1
    # Final answer was still emitted after hitting continuation cap
    text_events = [ev.data.get("text", "") for ev in events if ev.type is EventType.TEXT_DELTA]
    assert "".join(text_events) == f"draft {MAX_STEERING_CONTINUATIONS}"
    # The last steer remains pending in inbox (lifecycle.close_run will emit STEERING_DEFERRED)
    assert inbox.has_pending() is True


@pytest.mark.asyncio
async def test_without_steering_inbox_isolates_subagent():
    """Sub-agents wrapped in without_steering_inbox() cannot observe or drain the parent's inbox."""
    inbox = SteeringInbox()
    inbox.push("parent only steer")

    with use_steering_inbox(inbox):
        assert current_steering_inbox() is inbox
        with without_steering_inbox():
            assert current_steering_inbox() is None
            registry = ToolRegistry()
            llm = _ScriptedLLM([ChatResponse(text="subagent reply", tool_calls=[])])
            _ = [
                ev
                async for ev in react_event_loop(
                    llm,
                    [{"role": "user", "content": "subtask"}],
                    registry,
                )
            ]
        # Parent's steering message is still untouched!
        assert inbox.pending_count() == 1
        assert inbox.drain()[0].content == "parent only steer"


@pytest.mark.asyncio
async def test_pipeline_steering_reaches_active_and_subsequent_nodes():
    """H3: Steering pushed before node 0 and between node 0 -> node 1 is delivered to the LLM
    (never drained and dropped in pipeline.py) and accumulated across nodes."""
    from engine.execution.pipeline.backtrack import FailureLoopGuard
    from engine.execution.pipeline.gate import Gate, GateResult
    from engine.execution.pipeline.pipeline import run_pipeline
    from engine.execution.pipeline.skill_chain import SkillChain, SkillNode
    from engine.skill.loader import SkillBody, SkillMeta
    from engine.skill.registry import SkillRegistry

    inbox = SteeringInbox()
    inbox.push("keep functions under 20 lines")

    class _StepOneGate(Gate):
        async def check(self, output: str, context: dict) -> GateResult:
            # Push a second steer right between node 0 and node 1
            inbox.push("add docstring to output")
            return GateResult("pass", "ok")

    class _PassGate(Gate):
        async def check(self, output: str, context: dict) -> GateResult:
            return GateResult("pass", "ok")

    skill_registry = SkillRegistry()
    for name in ("step_one", "step_two"):
        skill_registry._skills[name] = SkillBody(
            meta=SkillMeta(name=name, description=f"Skill {name}"),
            content=f"Perform {name}.",
        )

    chain = SkillChain(
        nodes=[
            SkillNode(skill_name="step_one", gate=_StepOneGate()),
            SkillNode(skill_name="step_two", gate=_PassGate()),
        ],
    )

    llm = _ScriptedLLM([
        ChatResponse(text="step one output", tool_calls=[]),
        ChatResponse(text="step two output", tool_calls=[]),
    ])
    registry = ToolRegistry()
    base_messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "implement feature"},
    ]

    with use_steering_inbox(inbox):
        events = [
            ev
            async for ev in run_pipeline(
                chain,
                llm,
                "implement feature",
                base_messages,
                registry,
                skill_registry,
                None,
                FailureLoopGuard(),
                5,
                {},
            )
        ]

    # Exactly two STEERING_APPLIED events: one in step_one, one in step_two
    applied = [ev for ev in events if ev.type is EventType.STEERING_APPLIED]
    assert len(applied) == 2

    # Node 0 LLM call MUST actually contain the first steering note
    node0_text = "\n".join(str(m.get("content", "")) for m in llm.calls[0])
    assert "keep functions under 20 lines" in node0_text

    # Node 1 LLM call MUST contain BOTH accumulated steering from node 0 AND the new steer!
    node1_text = "\n".join(str(m.get("content", "")) for m in llm.calls[1])
    assert "keep functions under 20 lines" in node1_text
    assert "add docstring to output" in node1_text


@pytest.mark.asyncio
async def test_drain_b_after_length_continuation_does_not_duplicate_fragments():
    """M6: When a reply had a length continuation before Drain B triggers, only response.text
    (the newest fragment) is appended to conversation — earlier fragments are not duplicated."""
    registry = ToolRegistry()
    inbox = SteeringInbox()

    def on_call(idx: int, _messages):
        if idx == 1:
            # Steer arrives while model generates the continuation fragment
            inbox.push("add a conclusion")

    llm = _ScriptedLLM(
        [
            ChatResponse(text="PART_ONE ", finish_reason="length", tool_calls=[]),
            ChatResponse(text="PART_TWO", finish_reason="stop", tool_calls=[]),
            ChatResponse(text="FINAL_CONCLUDED", finish_reason="stop", tool_calls=[]),
        ],
        on_call=on_call,
    )

    with use_steering_inbox(inbox):
        _ = [
            ev
            async for ev in react_event_loop(
                llm,
                [{"role": "user", "content": "write essay"}],
                registry,
            )
        ]

    # Third LLM call should see PART_ONE exactly once across assistant messages
    assistant_contents = [
        str(m.get("content", ""))
        for m in llm.calls[2]
        if m.get("role") == "assistant"
    ]
    joined = "".join(assistant_contents)
    assert joined.count("PART_ONE") == 1
    assert joined.count("PART_TWO") == 1


@pytest.mark.asyncio
async def test_lifecycle_exception_closes_steering_and_emits_deferred_before_failed(tmp_path: Path):
    """M4: When execution raises an exception, undrained steers are immediately closed and
    emitted as STEERING_DEFERRED before FAILED/DONE rather than silently dropped."""
    from engine.execution.orchestration.lifecycle import run_stream_with_runtime
    from engine.execution.orchestration.runtime import (
        EngineRequest,
        RuntimeContext,
        RuntimeServices,
    )
    from engine.execution.steering import STEERING_BROKER
    from engine.safety.tool_guard import ToolGuard
    from engine.skill.registry import SkillRegistry

    data_dir = tmp_path / "data"
    profile_dir = data_dir / "agent"
    profile_dir.mkdir(parents=True)
    for fname in ("role.md", "style.md", "workflow.md", "context.md"):
        (profile_dir / fname).write_text(f"# {fname}\n", encoding="utf-8")
    work_dir = tmp_path / "ws"
    work_dir.mkdir()
    agents_dir = Path(__file__).resolve().parents[3] / "agents"

    session_id = "sess-exc-steer"

    class _ExplodingLLM:
        model = "test-model"
        provider = "openai"

        async def chat(self, messages, tools=None, **kwargs):
            # Submit a steer right before failing
            res = STEERING_BROKER.submit(session_id=session_id, content="save me on failure")
            assert res.accepted is True
            raise RuntimeError("boom provider crash")

        async def close(self):
            return None

    services = RuntimeServices(
        llm=_ExplodingLLM(),
        tool_registry=ToolRegistry(),
        skill_registry=SkillRegistry(),
        tool_guard=ToolGuard(tmp_path / "rules.json"),
    )
    runtime = RuntimeContext(
        agent_id="smith-1",
        agent_name="Smith",
        profile_dir=profile_dir,
        agents_dir=agents_dir,
        session_id=session_id,
    )
    request = EngineRequest(
        message="trigger crash",
        working_dir=str(work_dir),
        ephemeral=True,
    )

    stream = run_stream_with_runtime(request, runtime, services)
    events = [ev async for ev in stream.stream_events()]
    types = [ev.type for ev in events]

    assert EventType.STEERING_DEFERRED in types
    assert EventType.FAILED in types
    assert types.index(EventType.STEERING_DEFERRED) < types.index(EventType.FAILED)
    deferred = [ev for ev in events if ev.type is EventType.STEERING_DEFERRED][0]
    assert [item["content"] for item in deferred.data["items"]] == ["save me on failure"]
    # Broker must be closed for this session
    assert STEERING_BROKER.active_run_id(session_id) is None
