"""Termination regression tests: gate failures must never loop forever.

P0 回归背景：旧 FailureLoopGuard 用全局策略集合 + 输出 hash 计数，
不在 backtrack_map 的节点门禁一直不过时永远返回 "retry"，
run_pipeline 以相同 node_idx 无界重跑（每轮烧一整个 ReAct + 门禁调用）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from engine.execution.orchestration.agent_loop import run_agent_stream
from engine.execution.pipeline.backtrack import FailureLoopGuard, FailureSignature
from engine.execution.events import EventType, ExecutionEvent
from engine.execution.pipeline.gate import GateResult
from engine.execution.pipeline.pipeline import _evict_outputs_at_or_after, run_pipeline
from engine.execution.react.react_loop import react_event_loop
from engine.execution.react.budget import TOOL_CALL_BUDGET_MESSAGE, budget_exhausted_message
from engine.execution.pipeline.skill_chain import SkillChain, SkillNode
from engine.identity import IdentitySpec, RouteDecision
from engine.llm.client import ChatResponse
from engine.skill.loader import SkillBody, SkillMeta

_SMITH = IdentitySpec(
    id="smith", name="Smith", description="", prompt="",
    enabled_tools=None, enabled_skills=None, routes=(), is_default=True,
)
FEATURE_ROUTE = RouteDecision(_SMITH, "feature", "feature", score=1)

_RUBRIC_PASSING_TEXT = (
    "Completed the requested work with evidence in "
    "engine/execution/orchestration/agent_loop.py and enough detail for review."
)


class FakeLLM:
    async def chat(self, messages, tools=None, prefix_cache_key=None):
        return ChatResponse(text=_RUBRIC_PASSING_TEXT)


class FakeToolRegistry:
    def get_schemas(self):
        return []


class FakeSkillRegistry:
    def get(self, name):
        return SkillBody(meta=SkillMeta(name=name), content="Do the work.")


class AlwaysFailGate:
    async def check(self, output, context):
        return GateResult("fail", "never good enough")


def _collect(chain, execution_context=None):
    async def run():
        events = []
        async for event in run_agent_stream(
            FakeLLM(), "system prompt", "build a feature",
            FakeToolRegistry(), FakeSkillRegistry(),
            FEATURE_ROUTE, chain, FailureLoopGuard(),
            execution_context=execution_context,
        ):
            events.append(event)
        return events

    # 回归时该 run 永不结束——用超时把"无界循环"变成显式失败而非挂死。
    return asyncio.run(asyncio.wait_for(run(), timeout=10))


def test_failing_gate_without_backtrack_terminates_blocked(tmp_path: Path) -> None:
    chain = SkillChain([SkillNode("planning", AlwaysFailGate())])
    events = _collect(chain, execution_context={
        "agent_id": "a", "session_id": "sess-t", "_state_dir": str(tmp_path),
    })
    types = [e.type for e in events]

    assert EventType.BLOCKED in types
    assert types[-1] == EventType.DONE
    # 有界升级：retry 一次 + switch 改写 blocked，节点最多被执行 3 次
    assert types.count(EventType.SKILL_START) <= 3
    # blocked 终止后不允许留下 checkpoint 残骸
    assert not (tmp_path / "sessions" / ".state" / "sess-t.json").exists()


def test_question_without_pause_marker_waits_instead_of_retrying_a_gate(
    tmp_path: Path,
) -> None:
    class QuestionLLM:
        async def chat(self, messages, tools=None, prefix_cache_key=None):
            return ChatResponse(text="Which supported provider should we prioritize?")

    class GateMustNotRun:
        async def check(self, output, context):
            raise AssertionError("a user question must pause before the node gate")

    chain = SkillChain([
        SkillNode(
            "grilling",
            GateMustNotRun(),
            await_user_input_marker="<!-- agent-smith:await-user-input -->",
            infer_await_user_input_from_question=True,
        ),
    ])

    async def run():
        return [
            event
            async for event in run_agent_stream(
                QuestionLLM(),
                "system prompt",
                "research a provider",
                FakeToolRegistry(),
                FakeSkillRegistry(),
                FEATURE_ROUTE,
                chain,
                FailureLoopGuard(),
                execution_context={
                    "agent_id": "a",
                    "session_id": "sess-question",
                    "_state_dir": str(tmp_path),
                },
            )
        ]

    events = asyncio.run(run())

    assert EventType.AWAITING_INPUT in [event.type for event in events]
    assert EventType.GATE_RESULT not in [event.type for event in events]
    assert [
        event.data["status"]
        for event in events
        if event.type is EventType.SKILL_END
    ] == ["awaiting_input"]


def test_question_without_question_inference_still_runs_the_node_gate() -> None:
    class QuestionLLM:
        async def chat(self, messages, tools=None, prefix_cache_key=None):
            return ChatResponse(text="Review complete. Would you like a patch next?")

    class PassingGate:
        async def check(self, output, context):
            return GateResult("pass", "review report is complete")

    chain = SkillChain([
        SkillNode(
            "code-review",
            PassingGate(),
            await_user_input_marker="<!-- agent-smith:await-user-input -->",
        ),
    ])

    async def run():
        return [
            event
            async for event in run_agent_stream(
                QuestionLLM(),
                "system prompt",
                "review this diff",
                FakeToolRegistry(),
                FakeSkillRegistry(),
                FEATURE_ROUTE,
                chain,
                FailureLoopGuard(),
            )
        ]

    events = asyncio.run(run())

    assert EventType.GATE_RESULT in [event.type for event in events]
    assert EventType.AWAITING_INPUT not in [event.type for event in events]


def test_backtrack_target_missing_terminates_blocked() -> None:
    chain = SkillChain(
        [SkillNode("planning", AlwaysFailGate())],
        backtrack_map={"planning": "no-such-node"},
    )
    events = _collect(chain)

    blocked = [e for e in events if e.type == EventType.BLOCKED]
    assert blocked and "not found" in blocked[0].data["reason"]


def test_user_disabled_pipeline_skill_is_skipped_without_generic_fallback() -> None:
    class PassingGate:
        async def check(self, output, context):
            return GateResult("pass", "")

    async def run() -> list[ExecutionEvent]:
        events = []
        async for event in run_agent_stream(
            FakeLLM(), "system prompt", "build a feature",
            FakeToolRegistry(), FakeSkillRegistry(), FEATURE_ROUTE,
            SkillChain([SkillNode("planning", PassingGate())]), FailureLoopGuard(),
            disabled_skill_names=frozenset({"planning"}),
        ):
            events.append(event)
        return events

    events = asyncio.run(run())

    assert all(event.type is not EventType.SKILL_START for event in events)
    assert all(event.type is not EventType.SKILL_END for event in events)
    assert events[-1].type is EventType.DONE


def test_pipeline_with_a_missing_skill_falls_back_to_direct_react() -> None:
    """An incomplete workflow installation must not run its strict gates as generic ReAct."""

    class MissingSkillRegistry:
        def get(self, name):
            return None

    async def run() -> list[ExecutionEvent]:
        events: list[ExecutionEvent] = []
        async for event in run_agent_stream(
            FakeLLM(), "system prompt", "inspect the configured provider",
            FakeToolRegistry(), MissingSkillRegistry(), FEATURE_ROUTE,
            SkillChain([SkillNode("understanding", AlwaysFailGate())]), FailureLoopGuard(),
        ):
            events.append(event)
        return events

    events = asyncio.run(run())

    assert EventType.BLOCKED not in [event.type for event in events]
    assert not [event for event in events if event.type is EventType.SKILL_START]
    assert [event.data["text"] for event in events if event.type is EventType.TEXT_DELTA] == [
        _RUBRIC_PASSING_TEXT,
    ]


def test_guard_escalates_per_node() -> None:
    guard = FailureLoopGuard()
    # LLM 输出每次不同也必须按节点计数收敛（signature 只按节点 keying）
    assert guard.record(FailureSignature("node-a")) == "retry"
    assert guard.record(FailureSignature("node-a")) == "switch"
    assert guard.record(FailureSignature("node-a")) == "blocked"
    # 其他节点独立计数，不被 node-a 的失败历史污染
    assert guard.record(FailureSignature("node-b")) == "retry"


def test_truncation_does_not_split_tool_pairs() -> None:
    captured: dict = {}

    class RecordingLLM:
        async def chat(self, messages, tools=None):
            captured["messages"] = messages
            return ChatResponse(text="done")

    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
    ]
    # 42 条消息（> 硬顶 40），让 -KEEP_RECENT 切点落在一串 tool 结果中间
    for i in range(8):
        messages.append({
            "role": "assistant", "content": "",
            "tool_calls": [
                {"id": f"c{i}{j}", "type": "function",
                 "function": {"name": "t", "arguments": "{}"}}
                for j in range(4)
            ],
        })
        messages.extend(
            {"role": "tool", "tool_call_id": f"c{i}{j}", "content": "r"}
            for j in range(4)
        )

    async def run():
        async for _ in react_event_loop(RecordingLLM(), messages, FakeToolRegistry(), None, 5):
            pass

    asyncio.run(run())

    sent = captured["messages"]
    assert len(sent) < len(messages)  # 确认截断真的发生了
    _assert_tool_pairs_intact(sent)


def test_truncation_backs_past_system_hint_inside_tool_run() -> None:
    """TOOL_FAILURE_HINT 会夹在同轮 tool 结果之间（tool_calls 循环内 append）；
    截断回退只认 role=="tool" 时会在 system 提示处停下，留下孤儿 tool 消息。"""
    captured: dict = {}

    class RecordingLLM:
        async def chat(self, messages, tools=None):
            captured["messages"] = messages
            return ChatResponse(text="done")

    def tool_round(rid: str, n_tools: int) -> list[dict]:
        return [{
            "role": "assistant", "content": "",
            "tool_calls": [
                {"id": f"{rid}-{j}", "type": "function",
                 "function": {"name": "t", "arguments": "{}"}}
                for j in range(n_tools)
            ],
        }] + [
            {"role": "tool", "tool_call_id": f"{rid}-{j}", "content": "r"}
            for j in range(n_tools)
        ]

    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
    ]
    for i in range(3):
        messages.extend(tool_round(f"f{i}", 3))          # 2 + 3*4 = 14 条
    # 关键轮：4 个 tool_calls，前 3 个结果后插入 system 提示，再跟第 4 个结果
    hint_round = tool_round("x", 4)                       # [assistant, t0, t1, t2, t3]
    hint_round.insert(4, {"role": "system", "content": "tool failure hint"})
    messages.extend(hint_round)                           # 14..19，tool x-3 在 index 19
    while len(messages) < 46:                             # cut = 47-28 = 19，正落在 x-3 上
        messages.extend(tool_round(f"p{len(messages)}", 1))
    messages.append({"role": "user", "content": "continue"})  # 凑到 47 且结尾合法

    assert messages[19]["role"] == "tool" and messages[19]["tool_call_id"] == "x-3"
    assert messages[18]["role"] == "system"

    async def run():
        async for _ in react_event_loop(RecordingLLM(), messages, FakeToolRegistry(), None, 5):
            pass

    asyncio.run(run())

    sent = captured["messages"]
    assert len(sent) < len(messages)
    _assert_tool_pairs_intact(sent)


def _assert_tool_pairs_intact(sent: list[dict]) -> None:
    seen_call_ids: set[str] = set()
    for m in sent:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            seen_call_ids.update(tc["id"] for tc in m["tool_calls"])
        elif m.get("role") == "tool":
            # 每条 tool 消息必须能配到前文 assistant 的 tool_calls，否则 provider 400
            assert m["tool_call_id"] in seen_call_ids


class PassingGate:
    async def check(self, output, context):
        return GateResult("pass", "ok")


class RecordingLLM:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools=None, prefix_cache_key=None):
        self.calls.append([dict(m) for m in messages])
        return ChatResponse(text=next(self._responses))


# --- P5/P11: topology validation ------------------------------------------


def test_pipeline_rejects_duplicate_skill_names() -> None:
    chain = SkillChain([
        SkillNode("planning", PassingGate()),
        SkillNode("planning", AlwaysFailGate()),
    ])
    with pytest.raises(ValueError, match="duplicate skill names"):
        _collect(chain)


def test_pipeline_rejects_self_backtrack_target() -> None:
    chain = SkillChain(
        [SkillNode("planning", AlwaysFailGate())],
        backtrack_map={"planning": "planning"},
    )
    with pytest.raises(ValueError, match="must precede"):
        _collect(chain)


def test_pipeline_rejects_forward_backtrack_target() -> None:
    chain = SkillChain(
        [SkillNode("planning", PassingGate()), SkillNode("review", AlwaysFailGate())],
        backtrack_map={"planning": "review"},
    )
    with pytest.raises(ValueError, match="must precede"):
        _collect(chain)


# --- P1: stale outputs evicted on backtrack --------------------------------


def test_backtrack_evicts_committed_outputs_at_or_after_target() -> None:
    context = {"a_output": "A1", "b_output": "B1", "c_output": "C1", "other": 1}
    committed_output_index = {"a_output": 0, "b_output": 1, "c_output": 2}
    committed_provisional_output = {"a_output": True, "b_output": False, "c_output": True}

    _evict_outputs_at_or_after(
        context, committed_output_index, committed_provisional_output, target_idx=1
    )

    assert context == {"a_output": "A1", "other": 1}
    assert committed_output_index == {"a_output": 0}
    assert committed_provisional_output == {"a_output": True}


def test_backtrack_re_run_handoff_has_no_stale_future_node_output() -> None:
    """A backtracked re-run must not receive superseded outputs in its handoff."""
    llm = RecordingLLM([
        "PLAN-OUTPUT-A",
        "PLAN-OUTPUT-B",
        "C-OUTPUT-1",
        "C-OUTPUT-2",
        "PLAN-OUTPUT-A2",
        "PLAN-OUTPUT-B2",
        "C-OUTPUT-3",
    ])
    chain = SkillChain(
        [
            SkillNode("planning", PassingGate()),
            SkillNode("research", PassingGate()),
            SkillNode("implement", AlwaysFailGate()),
        ],
        backtrack_map={"implement": "planning"},
    )

    async def run():
        return [
            event
            async for event in run_agent_stream(
                llm,
                "system prompt",
                "build a feature",
                FakeToolRegistry(),
                FakeSkillRegistry(),
                FEATURE_ROUTE,
                chain,
                FailureLoopGuard(),
            )
        ]

    asyncio.run(run())

    # planning 在第 4 次调用重跑（回溯后）。B1（research 首轮产出）已在回溯时
    # 被逐出，不应再出现在 planning 重跑的 Prior Workflow Outputs 里。
    planning_rerun = "".join(str(m.get("content", "")) for m in llm.calls[4])
    assert "PLAN-OUTPUT-B" not in planning_rerun
    assert "PLAN-OUTPUT-A" not in planning_rerun


# --- P4: retry hint must not leak past a skipped backtrack target ----------


def test_backtrack_hint_does_not_leak_when_target_skill_is_disabled() -> None:
    class FailingThenPassingGate:
        def __init__(self) -> None:
            self.failures = 0

        async def check(self, output, context):
            self.failures += 1
            if self.failures <= 2:
                return GateResult("fail", "rejected", retry_hint="LEAK-HINT-MARKER")
            return GateResult("pass", "ok")

    llm = RecordingLLM([
        "P-1", "P-2", "P-3",  # planning 三次执行（前两次带 hint 重试/回溯）
        "F-1",               # final 节点
    ])
    chain = SkillChain(
        [
            SkillNode("review", PassingGate()),
            SkillNode("planning", FailingThenPassingGate()),
            SkillNode("final", PassingGate()),
        ],
        backtrack_map={"planning": "review"},
    )

    async def run():
        return [
            event
            async for event in run_agent_stream(
                llm,
                "system prompt",
                "build a feature",
                FakeToolRegistry(),
                FakeSkillRegistry(),
                FEATURE_ROUTE,
                chain,
                FailureLoopGuard(),
                disabled_skill_names=frozenset({"review"}),
            )
        ]

    events = asyncio.run(run())

    # planning 的第二次重试确实拿到了 hint（重试机制本身在起作用）……
    retry_call = "".join(str(m.get("content", "")) for m in llm.calls[1])
    assert "LEAK-HINT-MARKER" in retry_call
    # ……但 review 被禁用跳过时 hint 必须被清掉，planning 回溯后的重跑与
    # final 节点都不得把它当成自己的 rubric feedback。
    assert "LEAK-HINT-MARKER" not in "".join(
        str(m.get("content", "")) for m in llm.calls[2]
    )
    assert "LEAK-HINT-MARKER" not in "".join(
        str(m.get("content", "")) for m in llm.calls[3]
    )
    assert [event.data["status"] for event in events if event.type is EventType.SKILL_END] == [
        "retry", "passed", "passed",
    ]


# --- P3: budget message surfaced on incomplete/failed node ------------------


def _incomplete_node_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event_stream_factory):
    async def fake_execute_skill_events(
        skill, llm, tool_registry, messages, context, max_react_iters,
        tool_guard=None, provisional_lifecycle=True,
        react_event_loop_fn=None, prefix_cache_key=None,
    ):
        async for event in event_stream_factory():
            yield event

    import engine.skill.executor as skill_executor_module

    monkeypatch.setattr(skill_executor_module, "execute_skill_events", fake_execute_skill_events)

    async def run():
        return [
            event
            async for event in run_agent_stream(
                FakeLLM(),
                "system prompt",
                "build a feature",
                FakeToolRegistry(),
                FakeSkillRegistry(),
                FEATURE_ROUTE,
                SkillChain([SkillNode("planning", AlwaysFailGate())]),
                FailureLoopGuard(),
                execution_context={
                    "agent_id": "a",
                    "session_id": "sess-incomplete",
                    "_state_dir": str(tmp_path),
                },
            )
        ]

    return asyncio.run(run())


def test_incomplete_node_surfaces_engine_budget_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget_text = budget_exhausted_message(TOOL_CALL_BUDGET_MESSAGE)

    async def event_stream_factory():
        yield ExecutionEvent(EventType.TEXT_DELTA, {"text": budget_text})
        yield ExecutionEvent(EventType.INCOMPLETE, {"reason": "tool_call_budget"})

    events = _incomplete_node_events(tmp_path, monkeypatch, event_stream_factory)

    delta_texts = [e.data["text"] for e in events if e.type is EventType.TEXT_DELTA]
    assert budget_text in delta_texts
    assert [e.data["status"] for e in events if e.type is EventType.SKILL_END] == ["incomplete"]
    assert events[-1].type is EventType.DONE


def test_incomplete_node_does_not_surface_a_provider_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def event_stream_factory():
        yield ExecutionEvent(EventType.TEXT_DELTA, {"text": "UNGATED CONTENT-FILTERED DRAFT"})
        yield ExecutionEvent(EventType.INCOMPLETE, {"reason": "content_filter"})

    events = _incomplete_node_events(tmp_path, monkeypatch, event_stream_factory)

    assert not [e for e in events if e.type is EventType.TEXT_DELTA]
    assert EventType.INCOMPLETE in [e.type for e in events]


# --- P7: missing-skill fallback replaces the trailing user turn -------------


def test_missing_skill_fallback_replaces_user_turn_instead_of_appending() -> None:
    class RecordingMissingSkillLLM:
        def __init__(self) -> None:
            self.calls: list[list[dict]] = []

        async def chat(self, messages, tools=None, prefix_cache_key=None):
            self.calls.append([dict(m) for m in messages])
            return ChatResponse(text=_RUBRIC_PASSING_TEXT)

    class MissingSkillRegistry:
        def get(self, name):
            return None

    llm = RecordingMissingSkillLLM()

    async def run():
        return [
            event
            async for event in run_pipeline(
                SkillChain([SkillNode("planning", AlwaysFailGate())]),
                llm,
                "build a feature",
                [
                    {"role": "system", "content": "system prompt"},
                    {"role": "user", "content": "build a feature"},
                ],
                FakeToolRegistry(),
                MissingSkillRegistry(),
                None,
                FailureLoopGuard(),
                5,
                {},
            )
        ]

    asyncio.run(run())

    # 兜底 ReAct 复用 base_messages，必须替换末尾 user turn 而不是追加一条
    # 重复的 user 消息（部分 provider 拒绝连续 user turn）。
    for call in llm.calls:
        user_messages = [m for m in call if m.get("role") == "user"]
        assert len(user_messages) == 1
        assert user_messages[0]["content"] == "[Skill: planning] build a feature"


def test_missing_skill_fallback_merges_retry_hint_into_the_single_user_turn() -> None:
    """When the domain gate retries with a hint, the degraded (missing-skill)
    fallback must fold the hint into the single prefixed user turn rather than
    appending a second consecutive user message, which role-alternating
    providers (e.g. Anthropic) reject."""

    class RetryThenPassGate:
        def __init__(self) -> None:
            self.calls = 0

        async def check(self, output, context):
            self.calls += 1
            if self.calls == 1:
                return GateResult(
                    "retry",
                    "need more evidence",
                    retry_hint="Include the actual command output.",
                )
            return GateResult("pass", "ok")

    class RecordingMissingSkillLLM:
        def __init__(self) -> None:
            self.calls: list[list[dict]] = []

        async def chat(self, messages, tools=None, prefix_cache_key=None):
            self.calls.append([dict(m) for m in messages])
            return ChatResponse(text="evidence shown")

    class MissingSkillRegistry:
        def get(self, name):
            return None

    llm = RecordingMissingSkillLLM()
    gate = RetryThenPassGate()

    async def run():
        return [
            event
            async for event in run_pipeline(
                SkillChain([SkillNode("planning", gate)]),
                llm,
                "build a feature",
                [
                    {"role": "system", "content": "system prompt"},
                    {"role": "user", "content": "build a feature"},
                ],
                FakeToolRegistry(),
                MissingSkillRegistry(),
                None,
                FailureLoopGuard(),
                5,
                {},
            )
        ]

    events = asyncio.run(run())

    assert gate.calls == 2
    assert [e.type for e in events].count(EventType.BLOCKED) == 0
    for call in llm.calls:
        user_messages = [m for m in call if m.get("role") == "user"]
        assert len(user_messages) == 1, call
        content = user_messages[0]["content"]
        assert content.startswith("[Skill: planning] build a feature")
        if "Feedback" in content:
            assert "Include the actual command output." in content


# --- P6: provisional ledger survives checkpoint resume ----------------------


def test_resume_restores_provisional_ledger_for_already_streamed_final(
    tmp_path: Path,
) -> None:
    from engine.execution.pipeline.checkpoint import SessionCheckpoint, SessionStateManager

    SessionStateManager(tmp_path).save(SessionCheckpoint(
        run_id="crashedrun0000000000000000000001",
        agent_id="a",
        session_id="sess-prov",
        identity_id="smith",
        route_id="feature",
        skill_chain_index=0,  # planning already committed before the crash
        context={
            "user_message": "build a feature",
            "identity_id": "smith",
            "route_id": "feature",
            "agent_id": "a",
            "session_id": "sess-prov",
            "planning_output": "PLAN-FROM-CHECKPOINT",
        },
        timestamp="2026-07-15T00:00:00+00:00",
        working_dir=str(tmp_path.resolve()),
        provisional_outputs={"planning_output": True},
    ))

    async def run():
        return [
            event
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
                    "agent_id": "a",
                    "session_id": "sess-prov",
                    "_state_dir": str(tmp_path),
                    "_working_dir": str(tmp_path.resolve()),
                },
            )
        ]

    events = asyncio.run(run())

    final = [e for e in events if e.type is EventType.TEXT_DELTA]
    assert final and final[0].data["text"] == "PLAN-FROM-CHECKPOINT"
    # 恢复后 was_provisional 必须来自 checkpoint，否则已经流式渲染过的文本
    # 会被重复渲染一遍。
    assert final[0].data.get("already_streamed") is True
    assert not (tmp_path / "sessions" / ".state" / "sess-prov.json").exists()


# --- P9: checkpoint persistence is offloaded off the event loop -------------


def test_checkpoint_save_offloads_sync_io_to_a_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """json.dump + fsync + os.replace must run in a thread, never the loop."""
    import asyncio

    from engine.execution.pipeline import pipeline as pipeline_module
    from engine.execution.pipeline.pipeline_context import (
        CTX_AGENT_ID,
        CTX_IDENTITY_ID,
        CTX_ROUTE_ID,
        CTX_RUN_ID,
        CTX_SESSION_ID,
        CTX_STATE_DIR,
        CTX_WORKING_DIR,
    )

    offloaded: list[object] = []

    async def recording_to_thread(fn, *args, **kwargs):
        offloaded.append(fn)
        return fn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", recording_to_thread)

    context = {
        CTX_AGENT_ID: "a",
        CTX_SESSION_ID: "sess-p9",
        CTX_IDENTITY_ID: "smith",
        CTX_ROUTE_ID: "feature",
        CTX_STATE_DIR: str(tmp_path),
        CTX_WORKING_DIR: str(tmp_path),
        CTX_RUN_ID: "run-p9",
    }

    asyncio.run(pipeline_module._save_checkpoint(context, 0))

    assert offloaded, "checkpoint persistence must be offloaded via asyncio.to_thread"
    assert (tmp_path / "sessions" / ".state" / "sess-p9.json").is_file()
