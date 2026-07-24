"""Termination regression tests: gate failures must never loop forever.

P0 回归背景：旧 FailureLoopGuard 用全局策略集合 + 输出 hash 计数，
不在 backtrack_map 的节点门禁一直不过时永远返回 "retry"，
run_pipeline 以相同 node_idx 无界重跑（每轮烧一整个 ReAct + 门禁调用）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from engine.execution.orchestration.agent_loop import run_agent_stream
from engine.execution.pipeline.backtrack import FailureLoopGuard, FailureSignature
from engine.observability.events import EventType
from engine.execution.pipeline.gate import GateResult
from engine.execution.react.react_loop import react_event_loop
from engine.execution.pipeline.skill_chain import SkillChain, SkillNode
from engine.identity_catalog import IdentitySpec, RouteDecision
from engine.llm.client import ChatResponse
from engine.skill.loader import SkillBody, SkillMeta

_SMITH = IdentitySpec(
    id="smith", name="Smith", description="", prompt="",
    enabled_tools=None, enabled_skills=None, routes=(), is_default=True,
)
FEATURE_ROUTE = RouteDecision(_SMITH, "feature", "feature", score=1)

_RUBRIC_PASSING_TEXT = (
    "Completed the requested work with evidence in "
    "engine/execution/agent_loop.py and enough detail for review."
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


def test_guard_escalates_per_node_despite_varying_output_hash() -> None:
    guard = FailureLoopGuard()
    # LLM 输出每次不同（hash 不同）也必须按节点计数收敛
    assert guard.record(FailureSignature("node-a", "h1")) == "retry"
    assert guard.record(FailureSignature("node-a", "h2")) == "switch"
    assert guard.record(FailureSignature("node-a", "h3")) == "blocked"
    # 其他节点独立计数，不被 node-a 的失败历史污染
    assert guard.record(FailureSignature("node-b", "h4")) == "retry"


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
