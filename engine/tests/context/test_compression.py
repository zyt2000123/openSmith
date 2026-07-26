from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace

from engine.context.compression import (
    DEFAULT_CONTEXT_LIMIT,
    compact_history,
    compaction_policy_for_llm,
    compress,
    needs_compaction,
)
from engine.context import SessionSummaryStatus, summarize_session
from engine.llm.contracts import ModelLimits


def test_needs_compaction_uses_actual_conversation_size() -> None:
    conversation = [{"role": "system", "content": "x" * 300_000}]

    assert needs_compaction(conversation, context_limit=120_000)


def test_needs_compaction_stays_false_for_small_conversations() -> None:
    conversation = [{"role": "user", "content": "hello"}]

    assert not needs_compaction(conversation, context_limit=120_000)


def test_needs_compaction_accounts_for_cjk_density() -> None:
    # 90k 中文字符 ≈ 90k tokens，超过 84k（120k×0.7）阈值。
    # 旧 len//3 估成 30k 会漏判 → compact 迟迟不触发，超窗口后才醒。
    conversation = [{"role": "user", "content": "证" * 90_000}]

    assert needs_compaction(conversation, context_limit=120_000)


def test_needs_compaction_defaults_to_conservative_context_window() -> None:
    conversation = [{"role": "user", "content": "证" * (int(DEFAULT_CONTEXT_LIMIT * 0.7) + 1)}]

    assert needs_compaction(conversation)


def test_compaction_policy_reserves_output_and_safety_margin() -> None:
    llm = SimpleNamespace(context_window=8_192, max_output_tokens=4_096)

    input_budget, trigger_ratio = compaction_policy_for_llm(llm)

    assert input_budget + llm.max_output_tokens < llm.context_window
    assert trigger_ratio < 1.0


def test_compress_reserves_output_before_triggering_for_large_declared_windows() -> None:
    class LargeWindowLLM:
        context_window = 1_000_000
        context_window_declared = True

        async def chat(self, messages, tools=None):
            return SimpleNamespace(text="summary")

    budget, trigger_ratio = compaction_policy_for_llm(LargeWindowLLM())
    threshold = math.ceil(budget * trigger_ratio)
    below_limit = [{"role": "user", "content": "证" * (threshold - 1)}]
    at_limit = [{"role": "user", "content": "证" * threshold}]

    assert asyncio.run(compress(below_limit, LargeWindowLLM())) is below_limit
    assert asyncio.run(compress(at_limit, LargeWindowLLM())) is not at_limit


def test_compress_uses_safe_budget_when_window_is_undeclared() -> None:
    class UnconfiguredLLM:
        context_window = DEFAULT_CONTEXT_LIMIT
        context_window_declared = False

        async def chat(self, messages, tools=None):
            return SimpleNamespace(text="summary")

    budget, trigger_ratio = compaction_policy_for_llm(UnconfiguredLLM())
    threshold = math.ceil(budget * trigger_ratio)
    below_limit = [{"role": "user", "content": "证" * (threshold - 1)}]
    at_limit = [{"role": "user", "content": "证" * threshold}]

    assert asyncio.run(compress(below_limit, UnconfiguredLLM())) is below_limit
    assert asyncio.run(compress(at_limit, UnconfiguredLLM())) is not at_limit


def test_compact_history_keeps_tool_evidence_in_summary_input() -> None:
    # 压缩摘要的输入必须包含工具结果与工具调用意图，
    # 否则工具密集任务压缩一次就等于失忆。
    captured: dict = {}

    class FakeLLM:
        async def chat(self, messages, tools=None):
            captured["messages"] = messages
            return SimpleNamespace(text="summary")

    conversation = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "读取数据库配置"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "read_file"}}]},
        {"role": "tool", "content": "DATABASE_URL=postgres://demo"},
    ]

    asyncio.run(compact_history(conversation, FakeLLM()))

    blob = " ".join(m["content"] for m in captured["messages"])
    assert "DATABASE_URL" in blob   # 工具结果必须进摘要输入
    assert "read_file" in blob      # 工具调用意图也要保留


def test_compact_history_discards_empty_summary() -> None:
    # 摘要为空时整体替换历史 = 静默失忆；必须原样保留对话。
    class EmptyLLM:
        async def chat(self, messages, tools=None):
            return SimpleNamespace(text="   ")

    conversation = [
        {"role": "system", "content": "sp"},
        {"role": "user", "content": "hello"},
    ]
    result = asyncio.run(compact_history(conversation, EmptyLLM()))

    assert result is conversation


def test_compact_history_discards_truncated_summary() -> None:
    # finish_reason=length 说明摘要被截断，不能拿半句话当全部记忆。
    class TruncatedLLM:
        async def chat(self, messages, tools=None):
            return SimpleNamespace(text="partial summary", finish_reason="length")

    conversation = [{"role": "user", "content": "hi"}]
    result = asyncio.run(compact_history(conversation, TruncatedLLM()))

    assert result is conversation


def test_compact_history_preserves_all_leading_system_contracts() -> None:
    class SummaryLLM:
        async def chat(self, messages, tools=None):
            return SimpleNamespace(text="summary", finish_reason="stop")

    contracts = [
        {"role": "system", "content": "skill contract"},
        {"role": "system", "content": "agent contract"},
    ]
    result = asyncio.run(compact_history(
        [*contracts, {"role": "user", "content": "goal"}],
        SummaryLLM(),
    ))

    assert result[:2] == contracts


def test_session_summary_returns_typed_payload_without_synthetic_messages() -> None:
    class SummaryLLM:
        async def chat(self, messages, tools=None):
            return SimpleNamespace(text="typed summary", finish_reason="stop")

    result = asyncio.run(summarize_session(
        [{"role": "user", "content": "goal"}],
        SummaryLLM(),
    ))

    assert result.status is SessionSummaryStatus.COMPLETE
    assert result.usable
    assert result.summary == "typed summary"
    assert result.source_message_count == 1


def test_session_summary_fails_closed_when_its_own_prompt_cannot_fit() -> None:
    class TinyLLM:
        limits = ModelLimits(
            context_window=128,
            context_window_declared=True,
            max_output_tokens=64,
            max_output_tokens_declared=True,
        )

        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, tools=None):
            self.calls += 1
            return SimpleNamespace(text="must not be called", finish_reason="stop")

    llm = TinyLLM()
    result = asyncio.run(summarize_session(
        [{"role": "user", "content": "goal"}],
        llm,
    ))

    assert result.status is SessionSummaryStatus.UNFIT
    assert not result.usable
    assert llm.calls == 0
