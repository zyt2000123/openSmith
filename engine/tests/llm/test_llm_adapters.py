from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from common.yaml_utils import YamlConfigError
from engine.llm.adapters.anthropic import AnthropicAdapter
from engine.llm.adapters.openai import OpenAIAdapter
from engine.llm.adapters._http import MAX_STREAM_EVENT_BYTES
from engine.llm.adapters._retry import MAX_RETRY_AFTER_SECONDS, retry_after_seconds
from engine.llm.client import ProviderClient
from engine.llm.contracts import GEMINI_OPENAI_BASE_URL, LLMProviderConfig, LLMRequest
from engine.llm.events import ProviderEventType
from engine.llm.factory import create_llm_client, normalize_provider_name, supported_provider_names
from engine.llm.model_config import build_llm_client
from engine.llm.port import LLMPort


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


def _openai_client() -> ProviderClient:
    return ProviderClient(OpenAIAdapter(LLMProviderConfig(
        provider="openai", api_key="k",
        base_url="http://llm.test", model="m",
    )))


def _anthropic_config(**overrides: object) -> LLMProviderConfig:
    values: dict[str, object] = {
        "provider": "anthropic",
        "api_key": "anthropic-key",
        "base_url": "https://anthropic.test",
        "model": "claude-test",
        "max_output_tokens": 321,
    }
    values.update(overrides)
    return LLMProviderConfig(**values)  # type: ignore[arg-type]


def test_factory_selects_real_adapters_and_preserves_openai_aliases() -> None:
    anthropic = create_llm_client(_anthropic_config())
    openai = create_llm_client(_anthropic_config(
        provider="openai",
        base_url="https://openai.test/v1",
    ))
    gemini = create_llm_client(_anthropic_config(
        provider="gemini",
        base_url="",
        model="gemini-3.5-flash",
    ))
    legacy_openai = create_llm_client(_anthropic_config(
        provider="openai_compatible",
        base_url="https://openai.test/v1",
    ))
    try:
        assert isinstance(anthropic, LLMPort)
        assert anthropic.provider == "anthropic"
        assert type(anthropic.adapter).__name__ == "AnthropicAdapter"
        assert openai.provider == "openai"
        assert type(openai.adapter).__name__ == "OpenAIAdapter"
        assert legacy_openai.provider == "openai"
        assert gemini.provider == "gemini"
        assert type(gemini.adapter).__name__ == "GeminiAdapter"
        assert gemini.adapter.base_url == GEMINI_OPENAI_BASE_URL.rstrip("/")
        assert normalize_provider_name("openai") == "openai"
        assert normalize_provider_name("openai_compatible") == "openai"
        assert supported_provider_names() == (
            "anthropic",
            "gemini",
            "openai",
            "openai_compatible",
        )
    finally:
        asyncio.run(anthropic.close())
        asyncio.run(openai.close())
        asyncio.run(gemini.close())
        asyncio.run(legacy_openai.close())


def test_build_llm_client_rejects_unknown_provider() -> None:
    with pytest.raises(YamlConfigError, match="Unsupported LLM provider"):
        build_llm_client({
            "provider": "not-a-provider",
            "api_key": "key",
            "base_url": "https://example.test",
            "model": "model",
        })


@pytest.mark.parametrize(
    "base_url",
    ["http://relay.example/v1", "https://127.0.0.1/v1", "https://localhost/v1"],
)
def test_build_llm_client_rejects_insecure_or_private_endpoints(base_url: str) -> None:
    with pytest.raises(YamlConfigError, match="HTTPS|private or local"):
        build_llm_client({
            "provider": "openai",
            "api_key": "key",
            "base_url": base_url,
            "model": "model",
        })


def test_provider_retry_after_is_bounded() -> None:
    response = httpx.Response(
        429,
        headers={"Retry-After": "999"},
        request=httpx.Request("POST", "https://provider.test/messages"),
    )
    assert retry_after_seconds(response) == MAX_RETRY_AFTER_SECONDS


def _openai_fake_send(captured: dict[str, object]):
    """Return a fake send that captures the request body and returns a valid response."""
    async def fake_send(request, *, stream: bool = False):
        captured["body"] = json.loads(request.content)
        body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        return httpx.Response(200, request=request, stream=_SseStream([body]))
    return fake_send


def test_explicit_output_limit_is_forwarded_without_changing_openai_default() -> None:
    client = build_llm_client({
        "provider": "openai",
        "api_key": "key",
        "base_url": "https://openai.test/v1",
        "model": "model",
        "max_output_tokens": 123,
        "context_window": 1_000_000,
    })
    captured: dict[str, object] = {}
    client.adapter._http.send = _openai_fake_send(captured)  # type: ignore[attr-defined, assignment]
    try:
        response = asyncio.run(client.chat([{"role": "user", "content": "hello"}]))
    finally:
        asyncio.run(client.close())

    assert response.text == "ok"
    assert captured["body"]["max_tokens"] == 123
    assert client.context_window == 1_000_000


def test_openai_adapter_sends_normalized_output_limit_when_not_configured() -> None:
    client = build_llm_client({
        "provider": "openai",
        "api_key": "key",
        "base_url": "https://openai.test/v1",
        "model": "model",
    })
    captured: dict[str, object] = {}
    client.adapter._http.send = _openai_fake_send(captured)  # type: ignore[attr-defined, assignment]
    try:
        response = asyncio.run(client.chat([{"role": "user", "content": "hello"}]))
    finally:
        asyncio.run(client.close())

    assert response.text == "ok"
    assert captured["body"]["max_tokens"] == 4_096
    assert client.limits.max_output_tokens == 4_096
    assert client.limits.max_output_tokens_declared is False


def test_anthropic_adapter_translates_tools_conversation_and_response() -> None:
    adapter = AnthropicAdapter(_anthropic_config())
    captured: dict[str, object] = {}

    response_json = json.dumps({
        "content": [
            {"type": "thinking", "thinking": "check the tool result"},
            {"type": "text", "text": "I will look it up."},
            {
                "type": "tool_use",
                "id": "toolu-2",
                "name": "read_file",
                "input": {"path": "README.md"},
            },
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }).encode()

    async def fake_send(request, *, stream: bool = False):
        captured["url"] = str(request.url.raw_path, "ascii")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, request=request, stream=_SseStream([response_json]))

    adapter._http.send = fake_send  # type: ignore[assignment]
    try:
        response = asyncio.run(adapter.complete(LLMRequest(
            messages=[
                {"role": "system", "content": "Stay concise."},
                {"role": "user", "content": "Find the answer."},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "arguments": '{"query":"status"}',
                        },
                    }],
                },
                {"role": "tool", "tool_call_id": "call-1", "content": "green"},
                {"role": "user", "content": "Now answer."},
            ],
            tools=[{
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Look up a status.",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                },
            }],
        )))
    finally:
        asyncio.run(adapter.close())

    body = captured["body"]
    assert captured["url"] == "/v1/messages"
    assert isinstance(body, dict)
    assert body["system"] == "Stay concise."
    assert body["max_tokens"] == 321
    assert body["messages"][0] == {"role": "user", "content": "Find the answer."}
    assert body["messages"][1]["content"] == [{
        "type": "tool_use",
        "id": "call-1",
        "name": "lookup",
        "input": {"query": "status"},
    }]
    assert body["messages"][2]["content"] == [
        {"type": "tool_result", "tool_use_id": "call-1", "content": "green"},
        {"type": "text", "text": "Now answer."},
    ]
    assert body["tools"] == [{
        "name": "lookup",
        "description": "Look up a status.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
    }]
    assert response.text == "I will look it up."
    assert response.reasoning == "check the tool result"
    assert response.finish_reason == "tool_calls"
    assert response.tool_calls[0].arguments == {"path": "README.md"}
    assert response.usage == {"input_tokens": 11, "output_tokens": 7}


def test_anthropic_stream_normalizes_text_tools_usage_and_completion() -> None:
    adapter = AnthropicAdapter(_anthropic_config(max_output_tokens=128))
    client = ProviderClient(adapter)
    captured: dict[str, object] = {}

    async def fake_send(request, *, stream: bool):
        captured["request"] = request
        captured["stream"] = stream
        return httpx.Response(
            200,
            request=request,
            stream=_SseStream([
                b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":4}}}\n\n',
                b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello "}}\n\n',
                b'event: content_block_start\ndata: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu-1","name":"lookup","input":{}}}\n\n',
                b'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"query\\":\\""}}\n\n',
                b'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"status\\"}"}}\n\n',
                b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":9}}\n\n',
                b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
            ]),
        )

    adapter._http.send = fake_send  # type: ignore[assignment]

    async def collect():
        return [
            event
            async for event in client.chat_events([{"role": "user", "content": "hello"}])
        ]

    try:
        events = asyncio.run(collect())
    finally:
        asyncio.run(client.close())

    request = captured["request"]
    assert captured["stream"] is True
    assert request.url.path == "/v1/messages"
    assert request.headers["x-api-key"] == "anthropic-key"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert json.loads(request.content)["max_tokens"] == 128
    assert [event.type for event in events] == [
        ProviderEventType.RESPONSE_CREATED,
        ProviderEventType.OUTPUT_TEXT_DELTA,
        ProviderEventType.FUNCTION_CALL_ARGUMENTS_DELTA,
        ProviderEventType.FUNCTION_CALL_ARGUMENTS_DELTA,
        ProviderEventType.FUNCTION_CALL_ARGUMENTS_DELTA,
        ProviderEventType.USAGE,
        ProviderEventType.RESPONSE_COMPLETED,
    ]
    assert events[1].data == {"delta": "Hello "}
    assert events[2].data == {"index": 1, "id": "toolu-1", "name": "lookup"}
    assert events[3].data["arguments_delta"] == '{"query":"'
    assert events[4].data["arguments_delta"] == 'status"}'
    assert events[5].data == {"usage": {"input_tokens": 4, "output_tokens": 9}}
    assert events[-1].data == {
        "finish_reason": "tool_calls",
        "raw_finish_reason": "tool_use",
        "model": "claude-test",
    }


def test_anthropic_retry_discards_pre_content_events(monkeypatch) -> None:
    adapter = AnthropicAdapter(_anthropic_config())
    client = ProviderClient(adapter)
    calls = {"n": 0}

    async def no_wait(*_args) -> None:
        return None

    async def fake_send(request, *, stream: bool):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200,
                request=request,
                stream=_InterruptedSseStream(
                    b'event: message_start\ndata: {"type":"message_start",'
                    b'"message":{"usage":{"input_tokens":999}}}\n\n'
                    b'event: message_delta\ndata: {"type":"message_delta",'
                    b'"delta":{},"usage":{"output_tokens":0}}\n\n'
                ),
            )
        return httpx.Response(
            200,
            request=request,
            stream=_SseStream(
                [
                    b'event: message_start\ndata: {"type":"message_start",'
                    b'"message":{}}\n\n',
                    b'event: content_block_delta\ndata: {"type":"content_block_delta",'
                    b'"index":0,"delta":{"type":"text_delta","text":"Hello"}}\n\n',
                    b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
                ]
            ),
        )

    adapter._wait_for_retry = no_wait  # type: ignore[method-assign]
    adapter._http.send = fake_send  # type: ignore[assignment]

    try:
        events = asyncio.run(_collect_events_generic(client))
    finally:
        asyncio.run(client.close())

    assert calls["n"] == 2
    assert [event.type for event in events] == [
        ProviderEventType.RESPONSE_CREATED,
        ProviderEventType.OUTPUT_TEXT_DELTA,
        ProviderEventType.RESPONSE_COMPLETED,
    ]


def test_anthropic_retries_pre_content_overloaded_stream_error() -> None:
    adapter = AnthropicAdapter(_anthropic_config())
    client = ProviderClient(adapter)
    calls = {"n": 0}

    async def no_wait(*_args) -> None:
        return None

    async def fake_send(request, *, stream: bool):
        calls["n"] += 1
        chunks = (
            [
                b'event: error\ndata: {"type":"error","error":'
                b'{"type":"overloaded_error","message":"Overloaded"}}\n\n',
            ]
            if calls["n"] == 1
            else [
                b'event: content_block_delta\ndata: {"type":"content_block_delta",'
                b'"index":0,"delta":{"type":"text_delta","text":"Recovered"}}\n\n',
                b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
            ]
        )
        return httpx.Response(
            200,
            request=request,
            stream=_SseStream(chunks),
        )

    adapter._wait_for_retry = no_wait  # type: ignore[method-assign]
    adapter._http.send = fake_send  # type: ignore[assignment]

    try:
        events = asyncio.run(_collect_events_generic(client))
    finally:
        asyncio.run(client.close())

    assert calls["n"] == 2
    assert [event.type for event in events] == [
        ProviderEventType.RESPONSE_CREATED,
        ProviderEventType.OUTPUT_TEXT_DELTA,
        ProviderEventType.RESPONSE_COMPLETED,
    ]
    assert events[1].data == {"delta": "Recovered"}


def test_openai_retries_a_pre_content_truncated_stream() -> None:
    """流在产出任何内容前被切断(缺 [DONE])属于瞬时故障,应像 Anthropic 一样重试。"""
    adapter = OpenAIAdapter(LLMProviderConfig(
        provider="openai", api_key="k",
        base_url="https://openai.test/v1", model="m",
    ))
    client = ProviderClient(adapter)
    calls = {"n": 0}

    async def no_wait(*_args) -> None:
        return None

    async def fake_send(request, *, stream: bool):
        calls["n"] += 1
        if calls["n"] == 1:
            # A relay drops the connection before [DONE], with no content.
            return httpx.Response(
                200,
                request=request,
                stream=_SseStream([b'data: {"choices":[{"delta":{}}]}\n\n']),
            )
        return httpx.Response(
            200,
            request=request,
            stream=_SseStream([
                b'data: {"choices":[{"delta":{"content":"Recovered"}}]}\n\n',
                b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
                b"data: [DONE]\n\n",
            ]),
        )

    adapter._wait_for_retry = no_wait  # type: ignore[method-assign]
    adapter._http.send = fake_send  # type: ignore[assignment]

    try:
        events = asyncio.run(_collect_events_generic(client))
    finally:
        asyncio.run(client.close())

    assert calls["n"] == 2
    assert [event.type for event in events] == [
        ProviderEventType.RESPONSE_CREATED,
        ProviderEventType.OUTPUT_TEXT_DELTA,
        ProviderEventType.RESPONSE_COMPLETED,
    ]
    assert events[1].data == {"delta": "Recovered"}


def test_anthropic_moves_late_system_instruction_into_ordered_user_turn() -> None:
    system, messages = AnthropicAdapter._translate_messages([
        {"role": "system", "content": "Initial guidance."},
        {"role": "user", "content": "Start."},
        {"role": "assistant", "content": "Partial."},
        {"role": "system", "content": "Continue exactly."},
    ])

    assert system == "Initial guidance."
    assert messages == [
        {"role": "user", "content": "Start."},
        {"role": "assistant", "content": "Partial."},
        {"role": "user", "content": "Continue exactly."},
    ]


# ── New validation coverage ──────────────────────────────────────────────


def test_openai_response_size_cap_aborts_before_full_parse() -> None:
    from engine.llm.adapters._http import MAX_RESPONSE_BYTES as _MAX_RESPONSE_BYTES
    from engine.llm.contracts import LLMResponseError as _Err

    adapter = OpenAIAdapter(LLMProviderConfig(
        provider="openai", api_key="k",
        base_url="https://openai.test/v1", model="m",
    ))
    oversized = b"x" * (_MAX_RESPONSE_BYTES + 1)

    async def fake_send(request, *, stream: bool = False):
        return httpx.Response(200, request=request, stream=_SseStream([oversized]))

    adapter._http.send = fake_send  # type: ignore[assignment]
    with pytest.raises(_Err, match="exceeds"):
        asyncio.run(adapter.complete(LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
        )))
    asyncio.run(adapter.close())


def test_anthropic_response_size_cap() -> None:
    from engine.llm.adapters._http import MAX_RESPONSE_BYTES as _MAX_RESPONSE_BYTES
    from engine.llm.contracts import LLMResponseError as _Err

    adapter = AnthropicAdapter(_anthropic_config())
    oversized = b"x" * (_MAX_RESPONSE_BYTES + 1)

    async def fake_send(request, *, stream: bool = False):
        return httpx.Response(200, request=request, stream=_SseStream([oversized]))

    adapter._http.send = fake_send  # type: ignore[assignment]
    with pytest.raises(_Err, match="exceeds"):
        asyncio.run(adapter.complete(LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
        )))
    asyncio.run(adapter.close())


def test_openai_non_string_content_raises() -> None:
    from engine.llm.contracts import LLMResponseError as _Err

    client = _openai_client()
    body = json.dumps({"choices": [{"message": {"content": 42}}]}).encode()

    async def fake_send(request, *, stream: bool = False):
        return httpx.Response(200, request=request, stream=_SseStream([body]))

    client.adapter._http.send = fake_send  # type: ignore[assignment]
    with pytest.raises(_Err, match="content must be a string"):
        asyncio.run(client.chat([{"role": "user", "content": "hi"}]))
    asyncio.run(client.close())


@pytest.mark.parametrize("arguments", [None, ""])
def test_openai_accepts_empty_zero_argument_tool_calls(arguments: object) -> None:
    client = _openai_client()
    body = json.dumps({
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "current_time",
                        "arguments": arguments,
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
    }).encode()

    async def fake_send(request, *, stream: bool = False):
        return httpx.Response(
            200,
            request=request,
            stream=_SseStream([body]),
        )

    client.adapter._http.send = fake_send  # type: ignore[assignment]
    try:
        response = asyncio.run(client.chat([{"role": "user", "content": "time"}]))
    finally:
        asyncio.run(client.close())

    assert response.tool_calls[0].arguments == {}


def test_openai_stream_malformed_delta_raises() -> None:
    from engine.llm.contracts import LLMResponseError as _Err

    client = _openai_client()

    async def fake_send(request, *, stream: bool):
        return httpx.Response(200, request=request, stream=_SseStream([
            b'data: {"choices":[{"delta":"not-a-dict"}]}\n\n',
            b"data: [DONE]\n\n",
        ]))

    client.adapter._http.send = fake_send  # type: ignore[assignment]
    with pytest.raises(_Err, match="delta must be an object"):
        asyncio.run(_collect_events_generic(client))
    asyncio.run(client.close())


def test_openai_stream_rejects_an_oversized_sse_event() -> None:
    from engine.llm.contracts import LLMResponseError as _Err

    client = _openai_client()

    async def fake_send(request, *, stream: bool):
        return httpx.Response(
            200,
            request=request,
            stream=_SseStream([b"data: " + (b"x" * (MAX_STREAM_EVENT_BYTES + 1)) + b"\n\n"]),
        )

    client.adapter._http.send = fake_send  # type: ignore[assignment]
    with pytest.raises(_Err, match="stream event exceeds"):
        asyncio.run(_collect_events_generic(client))
    asyncio.run(client.close())


def test_anthropic_stream_malformed_delta_raises() -> None:
    from engine.llm.contracts import LLMResponseError as _Err

    adapter = AnthropicAdapter(_anthropic_config())
    client = ProviderClient(adapter)

    async def fake_send(request, *, stream: bool):
        return httpx.Response(200, request=request, stream=_SseStream([
            b'event: message_start\ndata: {"type":"message_start","message":{}}\n\n',
            b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":"bad"}\n\n',
        ]))

    adapter._http.send = fake_send  # type: ignore[assignment]
    with pytest.raises(_Err, match="content_block_delta must be an object"):
        asyncio.run(_collect_events_generic(client))
    asyncio.run(client.close())


def test_anthropic_stream_tool_use_missing_id_raises() -> None:
    from engine.llm.contracts import LLMResponseError as _Err

    adapter = AnthropicAdapter(_anthropic_config())
    client = ProviderClient(adapter)

    async def fake_send(request, *, stream: bool):
        return httpx.Response(200, request=request, stream=_SseStream([
            b'event: message_start\ndata: {"type":"message_start","message":{}}\n\n',
            b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","name":"lookup"}}\n\n',
        ]))

    adapter._http.send = fake_send  # type: ignore[assignment]
    with pytest.raises(_Err, match="missing id or name"):
        asyncio.run(_collect_events_generic(client))
    asyncio.run(client.close())


def test_openai_stream_failure_raises_llm_response_error() -> None:
    """Streaming HTTP failures are wrapped as LLMResponseError, not raw httpx."""
    from engine.llm.contracts import LLMResponseError as _Err

    client = _openai_client()

    async def fake_send(request, *, stream: bool):
        return httpx.Response(401, request=request, stream=_SseStream([b"unauthorized"]))

    client.adapter._http.send = fake_send  # type: ignore[assignment]
    with pytest.raises(_Err, match="HTTP 401"):
        asyncio.run(_collect_events_generic(client))
    asyncio.run(client.close())


def test_api_key_hidden_from_config_repr() -> None:
    config = LLMProviderConfig(
        provider="openai", api_key="sk-secret-key",
        base_url="https://api.test", model="m",
    )
    assert "sk-secret-key" not in repr(config)


async def _collect_events_generic(client):
    return [event async for event in client.chat_events([{"role": "user", "content": "hi"}])]
