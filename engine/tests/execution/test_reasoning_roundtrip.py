"""推理模型的 reasoning 必须随 assistant 消息回传，非推理模型不能多出字段。

真实故障（2026-07-25，e2e append 约 50% 复现）：
    OpenAI bad request: The `reasoning_content` in the thinking mode
    must be passed back to the API.

deepseek-v4-pro 这类推理模型返回 reasoning + tool_calls 后，engine 回灌 assistant
消息时丢掉了 reasoning，下一轮请求被 provider 整个拒收。636 条 mock 测试全绿也没抓到
—— 因为 mock 的 LLM 从不校验请求体合法性，这是 mock 层的结构性盲区。
"""

from __future__ import annotations

import asyncio

from engine.execution.react.react_loop import react_loop
from engine.llm.client import ChatResponse, ToolCallData
from engine.tool.registry import ToolRegistry


class _FakeLLM:
    """记录每次 chat 的 messages，用来检查回灌了什么。"""

    def __init__(self, responses: list[ChatResponse]) -> None:
        self.responses = list(responses)
        self.chat_calls: list[list[dict]] = []

    async def chat(self, messages, tools=None, prefix_cache_key=None):
        self.chat_calls.append([dict(message) for message in messages])
        return self.responses.pop(0)

    async def close(self) -> None:
        pass


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def ok():
        return "OK"

    registry.register("ok", "Succeeds", {}, ok)
    return registry


def _run(llm: _FakeLLM) -> None:
    asyncio.run(
        react_loop(
            llm,
            [{"role": "user", "content": "use the ok tool"}],
            _registry(),
            max_iters=3,
        )
    )


def _assistant_with_tool_calls(messages: list[dict]) -> dict:
    return next(
        message
        for message in messages
        if message.get("role") == "assistant" and message.get("tool_calls")
    )


def test_reasoning_is_passed_back_for_thinking_models():
    llm = _FakeLLM([
        ChatResponse(
            reasoning="先读文件，再决定怎么改",
            tool_calls=[ToolCallData(id="c1", name="ok", arguments={})],
        ),
        ChatResponse(text="done"),
    ])

    _run(llm)

    assert len(llm.chat_calls) >= 2, "工具调用后应有第二次请求"
    assistant = _assistant_with_tool_calls(llm.chat_calls[1])
    assert assistant.get("reasoning_content") == "先读文件，再决定怎么改"


def test_plain_models_get_no_reasoning_field():
    """非推理模型 reasoning 为空 —— 字段必须完全不出现，而不是空字符串。"""
    llm = _FakeLLM([
        ChatResponse(tool_calls=[ToolCallData(id="c1", name="ok", arguments={})]),
        ChatResponse(text="done"),
    ])

    _run(llm)

    assistant = _assistant_with_tool_calls(llm.chat_calls[1])
    assert "reasoning_content" not in assistant


def _assistant_without_tool_calls(messages: list[dict]) -> dict:
    return next(
        message
        for message in messages
        if message.get("role") == "assistant" and not message.get("tool_calls")
    )


def test_length_continuation_passes_reasoning_back():
    """finish_reason=length 的续写路径同样会再发一次请求 —— reasoning 不能丢。"""
    llm = _FakeLLM([
        ChatResponse(text="前半段", reasoning="被截断前的思考", finish_reason="length"),
        ChatResponse(text="后半段"),
    ])

    _run(llm)

    assert len(llm.chat_calls) >= 2
    assistant = _assistant_without_tool_calls(llm.chat_calls[1])
    assert assistant.get("reasoning_content") == "被截断前的思考"


def test_incomplete_final_repair_passes_reasoning_back():
    """残句修复路径也会再发一次请求，且必然发生在工具成功之后。"""
    llm = _FakeLLM([
        ChatResponse(tool_calls=[ToolCallData(id="c1", name="ok", arguments={})]),
        ChatResponse(text="让我再查一下。", reasoning="残句时的思考"),
        ChatResponse(text="真正的答案"),
    ])

    _run(llm)

    assert len(llm.chat_calls) >= 3
    assistant = _assistant_without_tool_calls(llm.chat_calls[2])
    assert assistant.get("reasoning_content") == "残句时的思考"


def test_reasoning_survives_multiple_tool_rounds():
    """连续两轮工具调用，每轮各自的 reasoning 都要跟着自己那条消息回传。"""
    llm = _FakeLLM([
        ChatResponse(
            reasoning="第一轮思考",
            tool_calls=[ToolCallData(id="c1", name="ok", arguments={})],
        ),
        ChatResponse(
            reasoning="第二轮思考",
            tool_calls=[ToolCallData(id="c2", name="ok", arguments={})],
        ),
        ChatResponse(text="done"),
    ])

    _run(llm)

    assert len(llm.chat_calls) >= 3
    final_messages = llm.chat_calls[2]
    reasonings = [
        message.get("reasoning_content")
        for message in final_messages
        if message.get("role") == "assistant" and message.get("tool_calls")
    ]
    assert reasonings == ["第一轮思考", "第二轮思考"]
