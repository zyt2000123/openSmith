from __future__ import annotations

import asyncio

import pytest

from engine.llm.contracts import ChatResponse, LLMResponseError
from engine.llm.vision_probe import (
    VisionSupport,
    probe_vision,
    reset_vision_cache,
    vision_support,
)


class _Provider:
    provider = "openai"
    model = "test-model"

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools=None, prefix_cache_key=None):
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return ChatResponse(text="ok")


def test_a_model_that_accepts_the_pixel_is_supported():
    llm = _Provider()

    assert asyncio.run(probe_vision(llm)) is VisionSupport.SUPPORTED


def test_the_probe_actually_sends_an_image():
    """不带图的探测证明不了任何事 —— 这条锁住探测请求的形状。"""
    llm = _Provider()

    asyncio.run(probe_vision(llm))

    content = llm.calls[0][0]["content"]
    assert isinstance(content, list)
    assert any(part.get("type") == "image_url" for part in content)


def test_a_4xx_rejection_means_unsupported():
    llm = _Provider(LLMResponseError("LLM request failed (HTTP 400) after 1 attempt(s)."))

    assert asyncio.run(probe_vision(llm)) is VisionSupport.UNSUPPORTED


@pytest.mark.parametrize(
    "message",
    [
        "LLM request failed (HTTP 401) after 1 attempt(s).",
        "LLM request failed (HTTP 403) after 1 attempt(s).",
        "LLM request failed (HTTP 429) after 1 attempt(s).",
        "LLM request failed (HTTP 500) after 1 attempt(s).",
        "LLM request failed after 3 attempts: connection reset",
    ],
)
def test_auth_ratelimit_and_transport_failures_are_unknown(message: str):
    """这些说明探测本身没跑成。断定不支持会因为一次网络抖动永久关掉图片功能。"""
    llm = _Provider(LLMResponseError(message))

    assert asyncio.run(probe_vision(llm)) is VisionSupport.UNKNOWN


def test_result_is_cached_per_model():
    reset_vision_cache()
    llm = _Provider()

    asyncio.run(vision_support(llm))
    asyncio.run(vision_support(llm))

    assert len(llm.calls) == 1, "同一个模型只该探测一次"


def test_unknown_is_not_cached():
    """探测失败是暂时的；缓存它等于让一次限流永久关掉图片功能。"""
    reset_vision_cache()
    llm = _Provider(LLMResponseError("LLM request failed (HTTP 429) after 1 attempt(s)."))

    asyncio.run(vision_support(llm))
    asyncio.run(vision_support(llm))

    assert len(llm.calls) == 2


def test_different_models_are_probed_separately():
    reset_vision_cache()
    first = _Provider()
    second = _Provider()
    second.model = "another-model"

    asyncio.run(vision_support(first))
    asyncio.run(vision_support(second))

    assert len(first.calls) == 1 and len(second.calls) == 1
