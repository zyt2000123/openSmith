"""Tests for the generation records emitted by ProviderClient."""

from __future__ import annotations

import asyncio

import httpx

from engine.llm.adapters.openai import OpenAIAdapter
from engine.llm.client import ProviderClient
from engine.llm.contracts import LLMProviderConfig
from engine.llm.observability import (
    GenerationRecord,
    generation_context,
    generation_sink,
    llm_purpose,
)


def _make_client(*, stream: bool = True) -> ProviderClient:
    return ProviderClient(
        OpenAIAdapter(LLMProviderConfig(
            provider="openai",
            api_key="k",
            base_url="http://llm.test",
            model="config-model",
        )),
        stream=stream,
    )


class _SseStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


_USAGE_JSON = (
    b'{"prompt_tokens":66,"completion_tokens":5,"total_tokens":71,'
    b'"prompt_tokens_details":{"cached_tokens":64},'
    b'"completion_tokens_details":{"reasoning_tokens":5}}'
)


def _client_with_stream_chunks(chunks: list[bytes]) -> ProviderClient:
    client = _make_client()

    async def send(request: httpx.Request, *, stream: bool = False) -> httpx.Response:
        return httpx.Response(200, request=request, stream=_SseStream(chunks))

    client.adapter._http.send = send  # type: ignore[assignment]
    return client


def _client_with_completion(payload: bytes, *, stream_mode: bool) -> ProviderClient:
    client = _make_client(stream=stream_mode)

    async def send(request: httpx.Request, *, stream: bool = False) -> httpx.Response:
        return httpx.Response(200, request=request, stream=_SseStream([payload]))

    client.adapter._http.send = send  # type: ignore[assignment]
    return client


_COMPLETION_JSON = (
    b'{"model":"served-model","choices":[{"message":{"content":"hi"},'
    b'"finish_reason":"stop"}],"usage":' + _USAGE_JSON + b"}"
)


def _collect(coro) -> list[GenerationRecord]:
    seen: list[GenerationRecord] = []

    async def sink(record: GenerationRecord) -> None:
        seen.append(record)

    async def run() -> None:
        with generation_sink(sink):
            await coro()

    asyncio.run(run())
    return seen


def test_chat_emits_one_normalized_record() -> None:
    client = _client_with_completion(_COMPLETION_JSON, stream_mode=True)

    async def call() -> None:
        with llm_purpose("routing"), generation_context(run_id="r1", session_id="s1"):
            await client.chat([{"role": "user", "content": "hi"}])

    records = _collect(call)
    assert len(records) == 1
    record = records[0]
    assert record.model == "served-model"
    assert record.purpose == "routing"
    assert record.run_id == "r1"
    assert record.session_id == "s1"
    assert record.stream is False
    assert record.ok is True
    assert record.ttft_ms is None
    assert record.total_ms >= 0
    assert record.usage["cache_read_tokens"] == 64
    assert record.usage["reasoning_tokens"] == 5
    assert record.usage["input_tokens"] == 66


def test_chat_failure_emits_failed_record() -> None:
    client = _make_client()

    async def send(request: httpx.Request, *, stream: bool = False) -> httpx.Response:
        return httpx.Response(400, request=request, stream=_SseStream([b'{"error":"bad"}']))

    client.adapter._http.send = send  # type: ignore[assignment]

    async def call() -> None:
        try:
            await client.chat([{"role": "user", "content": "hi"}])
        except Exception:
            pass

    records = _collect(call)
    assert len(records) == 1
    assert records[0].ok is False
    assert records[0].model == "config-model"
    assert records[0].usage["total_tokens"] == 0


def test_streaming_chat_events_emits_one_record_with_ttft() -> None:
    client = _client_with_stream_chunks([
        b'data: {"model":"served-model","choices":[{"delta":{"content":"He"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":' + _USAGE_JSON + b"}\n\n",
        b"data: [DONE]\n\n",
    ])

    async def call() -> None:
        with llm_purpose("main"):
            async for _ in client.chat_events([{"role": "user", "content": "hi"}]):
                pass

    records = _collect(call)
    assert len(records) == 1
    record = records[0]
    assert record.stream is True
    assert record.ok is True
    assert record.purpose == "main"
    assert record.model == "served-model"
    assert record.ttft_ms is not None
    assert record.usage["cache_read_tokens"] == 64


def test_interrupted_stream_emits_failed_record() -> None:
    class _Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"He"}}]}\n\n'
            raise httpx.ReadError("stream interrupted")

        async def aclose(self) -> None:
            return None

    client = _make_client()

    async def send(request: httpx.Request, *, stream: bool = False) -> httpx.Response:
        return httpx.Response(200, request=request, stream=_Interrupted())

    client.adapter._http.send = send  # type: ignore[assignment]

    async def call() -> None:
        try:
            async for _ in client.chat_events([{"role": "user", "content": "hi"}]):
                pass
        except Exception:
            pass

    records = _collect(call)
    assert len(records) == 1
    assert records[0].ok is False


def test_non_streaming_chat_events_does_not_double_emit() -> None:
    client = _client_with_completion(_COMPLETION_JSON, stream_mode=False)

    async def call() -> None:
        async for _ in client.chat_events([{"role": "user", "content": "hi"}]):
            pass

    records = _collect(call)
    assert len(records) == 1
    assert records[0].stream is False
    assert records[0].model == "served-model"


def test_client_exposes_config_model() -> None:
    assert _make_client().model == "config-model"
