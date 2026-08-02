from __future__ import annotations

import asyncio
import json as json_mod

import httpx
import pytest

from engine.llm.adapters.openai import OpenAIAdapter
from engine.llm.client import ProviderClient
from engine.llm.contracts import (
    LLMContextLengthError,
    LLMProviderConfig,
    LLMResponseError,
    LLMTimeouts,
)
from engine.llm.adapters.gemini import GeminiAdapter
from engine.llm.events import ProviderEventType


def _make_client(timeouts: LLMTimeouts | None = None) -> ProviderClient:
    return ProviderClient(OpenAIAdapter(LLMProviderConfig(
        provider="openai",
        api_key="k",
        base_url="http://llm.test",
        model="m",
        timeouts=timeouts or LLMTimeouts(),
    )))


def test_provider_client_exposes_normalized_model_limits() -> None:
    client = ProviderClient(OpenAIAdapter(LLMProviderConfig(
        provider="openai",
        api_key="k",
        base_url="http://llm.test",
        model="m",
        context_window=16_384,
        max_output_tokens=2_048,
    )))
    try:
        assert client.limits.context_window == 16_384
        assert client.limits.context_window_declared is True
        assert client.limits.max_output_tokens == 2_048
        assert client.max_output_tokens_declared is True
    finally:
        asyncio.run(client.close())


class _SseStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


class _InterruptedSseStream(httpx.AsyncByteStream):
    def __init__(self, first_chunk: bytes) -> None:
        self._first_chunk = first_chunk

    async def __aiter__(self):
        yield self._first_chunk
        raise httpx.ReadError("stream interrupted")

    async def aclose(self) -> None:
        return None


def _client_with_post(post_fn) -> ProviderClient:
    """Adapt old-style fake_post(url, json) -> Response into send()-based mock."""
    client = _make_client()

    async def wrapped_send(request, *, stream: bool = False):
        url = str(request.url)
        body = json_mod.loads(request.content) if request.content else {}
        orig = await post_fn(url, body)
        return httpx.Response(
            orig.status_code,
            headers=dict(orig.headers),
            request=request,
            stream=_SseStream([orig.content]),
        )

    client.adapter._http.send = wrapped_send  # type: ignore[assignment]
    return client


def _client_with_send(send_fn) -> ProviderClient:
    client = _make_client()
    client.adapter._http.send = send_fn  # type: ignore[assignment]
    return client


def _successful_stream_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        request=request,
        stream=_SseStream([
            b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
            b"data: [DONE]\n\n",
        ]),
    )


def _run_request(client: ProviderClient) -> None:
    try:
        asyncio.run(client.adapter._request({"model": "m", "messages": []}))
    except LLMResponseError:
        pass
    finally:
        asyncio.run(client.close())


def _capture_retry_delays(monkeypatch) -> list[float]:
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("engine.llm.adapters._retry.asyncio.sleep", fake_sleep)
    return delays


def test_request_does_not_retry_on_400() -> None:
    calls = {"n": 0}

    async def fake_post(url, json):
        calls["n"] += 1
        return httpx.Response(
            400,
            request=httpx.Request("POST", "http://llm.test/chat/completions"),
            text="bad request",
        )

    _run_request(_client_with_post(fake_post))

    assert calls["n"] == 1   # 400 是确定性错误，重试无意义


def test_request_retries_on_500(monkeypatch) -> None:
    _capture_retry_delays(monkeypatch)
    calls = {"n": 0}

    async def fake_post(url, json):
        calls["n"] += 1
        return httpx.Response(
            500,
            request=httpx.Request("POST", "http://llm.test/chat/completions"),
            text="server error",
        )

    _run_request(_client_with_post(fake_post))

    assert calls["n"] == 3   # 5xx 仍重试到上限（回归保护）


def test_request_uses_exponential_backoff_for_429(monkeypatch) -> None:
    retry_delays = _capture_retry_delays(monkeypatch)
    calls = {"n": 0}

    async def fake_post(url, json):
        calls["n"] += 1
        return httpx.Response(
            429,
            request=httpx.Request("POST", "http://llm.test/chat/completions"),
            text="rate limited",
        )

    _run_request(_client_with_post(fake_post))

    assert calls["n"] == 3
    assert retry_delays == [1, 2]


def test_request_honors_retry_after_header(monkeypatch) -> None:
    retry_delays = _capture_retry_delays(monkeypatch)
    calls = {"n": 0}

    async def fake_post(url, json):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "7"},
                request=httpx.Request("POST", "http://llm.test/chat/completions"),
            )
        return httpx.Response(
            200,
            request=httpx.Request("POST", "http://llm.test/chat/completions"),
            json={"choices": [{"message": {}}]},
        )

    client = _client_with_post(fake_post)
    try:
        asyncio.run(client.chat([{"role": "user", "content": "hello"}]))
    finally:
        asyncio.run(client.close())

    assert calls["n"] == 2
    assert retry_delays == [7.0]


def test_request_failure_does_not_surface_provider_error_body() -> None:
    """Remote error bodies can contain secrets and must not reach callers."""
    async def fake_post(url, json):
        return httpx.Response(
            400,
            request=httpx.Request("POST", "http://llm.test/chat/completions"),
            text='{"error":{"message":"model not found"}}',
        )

    client = _client_with_post(fake_post)
    try:
        with pytest.raises(LLMResponseError) as exc_info:
            asyncio.run(client.chat([{"role": "user", "content": "hello"}]))
        assert "model not found" not in str(exc_info.value)
    finally:
        asyncio.run(client.close())


def test_request_maps_context_rejection_to_typed_sanitized_error() -> None:
    async def fake_post(url, json):
        return httpx.Response(
            400,
            request=httpx.Request("POST", "http://llm.test/chat/completions"),
            json={
                "error": {
                    "code": "context_length_exceeded",
                    "message": "secret prompt payload is too long",
                },
            },
        )

    client = _client_with_post(fake_post)
    try:
        with pytest.raises(LLMContextLengthError) as exc_info:
            asyncio.run(client.chat([{"role": "user", "content": "hello"}]))
    finally:
        asyncio.run(client.close())

    assert exc_info.value.http_status == 400
    assert exc_info.value.provider_code == "context_length_exceeded"
    assert "secret prompt payload" not in str(exc_info.value)


def test_chat_reports_malformed_tool_arguments_as_provider_error() -> None:
    async def fake_post(url, json):
        return httpx.Response(
            200,
            request=httpx.Request("POST", "http://llm.test/chat/completions"),
            json={
                "choices": [{
                    "message": {
                        "tool_calls": [{
                            "id": "call-1",
                            "function": {"name": "read_file", "arguments": "{"},
                        }],
                    },
                }],
            },
        )

    client = _client_with_post(fake_post)
    try:
        with pytest.raises(LLMResponseError, match="invalid JSON tool-call arguments"):
            asyncio.run(client.chat([{"role": "user", "content": "hello"}]))
    finally:
        asyncio.run(client.close())


def test_chat_preserves_provider_finish_reason() -> None:
    async def fake_post(url, json):
        return httpx.Response(
            200,
            request=httpx.Request("POST", "http://llm.test/chat/completions"),
            json={
                "choices": [{
                    "message": {"content": "partial answer"},
                    "finish_reason": "length",
                }],
            },
        )

    client = _client_with_post(fake_post)
    try:
        response = asyncio.run(client.chat([{"role": "user", "content": "hello"}]))
    finally:
        asyncio.run(client.close())

    assert response.text == "partial answer"
    assert response.finish_reason == "length"


def test_client_uses_distinct_non_stream_and_stream_timeouts() -> None:
    selected_timeouts = LLMTimeouts(
        connect=1.0,
        read=2.0,
        stream_read=3.0,
        write=4.0,
        pool=5.0,
    )
    captured: dict[str, object] = {}

    async def fake_send(request, *, stream: bool = False):
        captured["timeout"] = request.extensions.get("timeout")
        body = json_mod.dumps({"choices": [{"message": {}}]}).encode()
        return httpx.Response(200, request=request, stream=_SseStream([body]))

    client = _make_client(selected_timeouts)
    client.adapter._http.send = fake_send  # type: ignore[assignment]
    try:
        asyncio.run(client.chat([{"role": "user", "content": "hello"}]))
    finally:
        asyncio.run(client.close())

    non_stream_timeout = captured["timeout"]
    assert non_stream_timeout == {"connect": 1.0, "read": 2.0, "write": 4.0, "pool": 5.0}
    assert client.adapter._http.timeout.read == 3.0


def test_chat_events_exposes_typed_provider_deltas_and_completion() -> None:
    async def fake_send(request, *, stream: bool):
        return httpx.Response(
            200,
            request=request,
            stream=_SseStream([
                b'data:{"choices":[{"delta":{"content":"Hello "}}]}\n\n',
                (
                    b'data: {"choices":[{"delta":{"tool_calls":[{'
                    b'"index":0,"id":"call-1","function":{'
                    b'"name":"read_file","arguments":"{\\"path\\":\\""}}]}}]}\n\n'
                ),
                (
                    b'data: {"choices":[{"delta":{"tool_calls":[{'
                    b'"index":0,"function":{"arguments":"README.md\\"}"}}]}}]}\n\n'
                ),
                b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n',
                b"data: [DONE]\n\n",
            ]),
        )

    client = _client_with_send(fake_send)
    try:
        events = asyncio.run(_collect_events(client))
    finally:
        asyncio.run(client.close())

    assert [event.type for event in events] == [
        ProviderEventType.RESPONSE_CREATED,
        ProviderEventType.OUTPUT_TEXT_DELTA,
        ProviderEventType.FUNCTION_CALL_ARGUMENTS_DELTA,
        ProviderEventType.FUNCTION_CALL_ARGUMENTS_DELTA,
        ProviderEventType.RESPONSE_COMPLETED,
    ]
    assert events[1].data == {"delta": "Hello "}
    assert events[2].data["name"] == "read_file"
    assert events[2].data["arguments_delta"] == '{"path":"'
    assert events[3].data["arguments_delta"] == 'README.md"}'
    assert events[-1].data == {
        "finish_reason": "tool_calls",
        "raw_finish_reason": "tool_calls",
        "model": "m",
    }


def test_non_streaming_client_emits_normalized_events_without_sse() -> None:
    client = _make_client()
    client.stream = False
    calls: list[bool] = []

    async def fake_send(request, *, stream: bool):
        calls.append(stream)
        body = json_mod.dumps({"choices": [{"message": {"content": "complete"}, "finish_reason": "stop"}]}).encode()
        return httpx.Response(200, request=request, stream=_SseStream([body]))

    client.adapter._http.send = fake_send  # type: ignore[assignment]
    try:
        events = asyncio.run(_collect_events(client))
    finally:
        asyncio.run(client.close())

    assert calls == [True]
    assert [event.type for event in events] == [
        ProviderEventType.RESPONSE_CREATED,
        ProviderEventType.OUTPUT_TEXT_DELTA,
        ProviderEventType.RESPONSE_COMPLETED,
    ]
    assert events[1].data == {"delta": "complete"}


def test_client_rejects_unsupported_prefix_cache_key() -> None:
    client = ProviderClient(GeminiAdapter(LLMProviderConfig(
        provider="gemini",
        api_key="key",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        model="gemini-test",
    )))
    try:
        with pytest.raises(LLMResponseError, match="prefix cache key"):
            asyncio.run(client.chat([{"role": "user", "content": "hello"}], prefix_cache_key="cache-key"))
    finally:
        asyncio.run(client.close())


def test_streaming_client_forwards_prefix_cache_key_to_adapter_body() -> None:
    captured: dict[str, object] = {}

    async def fake_send(request, *, stream: bool):
        captured["body"] = json_mod.loads(request.content)
        return _successful_stream_response(request)

    async def collect(client: ProviderClient):
        return [
            event
            async for event in client.chat_events(
                [{"role": "user", "content": "hello"}],
                prefix_cache_key="stable-prefix",
            )
        ]

    client = _client_with_send(fake_send)
    try:
        asyncio.run(collect(client))
    finally:
        asyncio.run(client.close())

    assert captured["body"]["prefix_cache_key"] == "stable-prefix"
    assert "extra_body" not in captured["body"]


def test_streaming_request_requests_usage_inclusion() -> None:
    """Streaming must set stream_options.include_usage so the provider emits
    the terminal usage chunk; otherwise all streaming cost accounting is zero."""
    captured: dict[str, object] = {}

    async def fake_send(request, *, stream: bool):
        captured["body"] = json_mod.loads(request.content)
        return _successful_stream_response(request)

    client = _client_with_send(fake_send)
    try:
        asyncio.run(_collect_events(client))
    finally:
        asyncio.run(client.close())

    assert captured["body"]["stream"] is True
    assert captured["body"]["stream_options"] == {"include_usage": True}


def test_non_streaming_request_omits_include_usage() -> None:
    """stream_options is a streaming-only field; non-streaming bodies keep the
    pre-change shape so relays that reject unknown fields stay compatible."""
    captured: dict[str, object] = {}

    async def fake_send(request, *, stream: bool):
        captured["body"] = json_mod.loads(request.content)
        return httpx.Response(
            200,
            request=request,
            stream=_SseStream([
                json_mod.dumps({
                    "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
                }).encode(),
            ]),
        )

    client = _client_with_send(fake_send)
    try:
        asyncio.run(client.chat([{"role": "user", "content": "hello"}]))
    finally:
        asyncio.run(client.close())

    assert captured["body"]["stream"] is False
    assert "stream_options" not in captured["body"]


def test_chat_events_retries_429_before_content(monkeypatch) -> None:
    retry_delays = _capture_retry_delays(monkeypatch)
    calls = {"n": 0}

    async def fake_send(request, *, stream: bool):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, request=request, text="rate limited")
        return _successful_stream_response(request)

    client = _client_with_send(fake_send)
    try:
        events = asyncio.run(_collect_events(client))
    finally:
        asyncio.run(client.close())

    assert calls["n"] == 2
    assert retry_delays == [1]
    assert [event.type for event in events] == [
        ProviderEventType.RESPONSE_CREATED,
        ProviderEventType.OUTPUT_TEXT_DELTA,
        ProviderEventType.RESPONSE_COMPLETED,
    ]


def test_chat_events_honors_retry_after_before_content(monkeypatch) -> None:
    retry_delays = _capture_retry_delays(monkeypatch)
    calls = {"n": 0}

    async def fake_send(request, *, stream: bool):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "4"}, request=request)
        return _successful_stream_response(request)

    client = _client_with_send(fake_send)
    try:
        events = asyncio.run(_collect_events(client))
    finally:
        asyncio.run(client.close())

    assert calls["n"] == 2
    assert retry_delays == [4.0]
    assert events[-1].type == ProviderEventType.RESPONSE_COMPLETED


def test_chat_events_retries_transport_error_before_content(monkeypatch) -> None:
    retry_delays = _capture_retry_delays(monkeypatch)
    calls = {"n": 0}

    async def fake_send(request, *, stream: bool):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection failed", request=request)
        return _successful_stream_response(request)

    client = _client_with_send(fake_send)
    try:
        events = asyncio.run(_collect_events(client))
    finally:
        asyncio.run(client.close())

    assert calls["n"] == 2
    assert retry_delays == [1]
    assert events[-1].type == ProviderEventType.RESPONSE_COMPLETED


def test_chat_events_discards_pre_content_events_from_failed_attempt(
    monkeypatch,
) -> None:
    retry_delays = _capture_retry_delays(monkeypatch)
    calls = {"n": 0}
    failed_attempt = (
        b'data: {"choices":[],"usage":{"prompt_tokens":999,'
        b'"completion_tokens":0,"total_tokens":999}}\n\n'
    )

    async def fake_send(request, *, stream: bool):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200,
                request=request,
                stream=_InterruptedSseStream(failed_attempt),
            )
        return _successful_stream_response(request)

    client = _client_with_send(fake_send)
    try:
        events = asyncio.run(_collect_events(client))
    finally:
        asyncio.run(client.close())

    assert calls["n"] == 2
    assert retry_delays == [1]
    assert [event.type for event in events] == [
        ProviderEventType.RESPONSE_CREATED,
        ProviderEventType.OUTPUT_TEXT_DELTA,
        ProviderEventType.RESPONSE_COMPLETED,
    ]


def test_usage_only_sse_events_each_get_an_independent_byte_budget() -> None:
    padding = "x" * 1000
    usage_event = (
        "data: "
        + json_mod.dumps(
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 0,
                    "total_tokens": 1,
                    "padding": padding,
                },
            }
        )
        + "\n\n"
    ).encode()
    chunks = [usage_event for _ in range(1100)]
    chunks.append(b"data: [DONE]\n\n")

    async def fake_send(request, *, stream: bool):
        return httpx.Response(
            200,
            request=request,
            stream=_SseStream(chunks),
        )

    client = _client_with_send(fake_send)
    try:
        events = asyncio.run(_collect_events(client))
    finally:
        asyncio.run(client.close())

    assert events[0].type == ProviderEventType.RESPONSE_CREATED
    assert sum(event.type == ProviderEventType.USAGE for event in events) == 1100
    assert events[-1].type == ProviderEventType.RESPONSE_COMPLETED


@pytest.mark.parametrize(
    "first_chunk",
    [
        b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
        (
            b'data: {"choices":[{"delta":{"tool_calls":[{'
            b'"index":0,"id":"call-1","function":{'
            b'"name":"read_file","arguments":"{}"}}]}}]}\n\n'
        ),
        b'data: {"choices":[{"delta":{"reasoning_content":"thinking..."}}]}\n\n',
    ],
)
def test_chat_events_does_not_retry_after_content_delta(monkeypatch, first_chunk: bytes) -> None:
    retry_delays = _capture_retry_delays(monkeypatch)
    calls = {"n": 0}

    async def fake_send(request, *, stream: bool):
        calls["n"] += 1
        return httpx.Response(
            200,
            request=request,
            stream=_InterruptedSseStream(first_chunk),
        )

    client = _client_with_send(fake_send)
    try:
        with pytest.raises(LLMResponseError):
            asyncio.run(_collect_events(client))
    finally:
        asyncio.run(client.close())

    assert calls["n"] == 1
    assert retry_delays == []


async def _collect_events(client: ProviderClient):
    return [event async for event in client.chat_events([{"role": "user", "content": "hello"}])]
