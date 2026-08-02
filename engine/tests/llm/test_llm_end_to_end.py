"""End-to-end tests for the llm module.

These drive the real :class:`engine.llm.client.ProviderClient` — over a
scripted OpenAI/Anthropic-compatible HTTP transport — through the canonical
:func:`engine.execution.react.react_loop.react_event_loop`.  A green run
therefore exercises the whole chain: config-shaped client -> adapter SSE
parsing -> normalized events -> response accumulator -> tool dispatch -> usage
/observability accounting -> (optionally) record & replay.

No test here touches the network; every provider call is served by a fake
``_http.send``.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from engine.execution.events import EventType
from engine.execution.react.react_loop import react_event_loop
from engine.execution.run_signature import signature_diff, signature_of
from engine.llm.adapters.anthropic import AnthropicAdapter
from engine.llm.adapters.openai import OpenAIAdapter
from engine.llm.client import ProviderClient
from engine.llm.contracts import LLMProviderConfig
from engine.llm.observability import (
    GenerationRecord,
    generation_context,
    generation_sink,
    llm_purpose,
)
from engine.llm.replay import RecordingLLM, ReplayLLM, load_recording
from engine.tool.registry import ToolRegistry


# ── scripted transport helpers ────────────────────────────────────────────


class _SseStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


def _sse(chunk: dict) -> bytes:
    return b"data: " + json.dumps(chunk, ensure_ascii=False).encode() + b"\n\n"


def _chunks_text(text: str, *, usage: dict | None = None) -> list[bytes]:
    """OpenAI SSE chunks for one plain-text assistant turn."""
    chunks = [_sse({"choices": [{"delta": {"role": "assistant", "content": text}}]})]
    final: dict = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
    if usage:
        final["usage"] = usage
    chunks.append(_sse(final))
    chunks.append(b"data: [DONE]\n\n")
    return chunks


def _chunks_tool_call(
    call_id: str,
    name: str,
    arguments: dict,
    *,
    usage: dict | None = None,
) -> list[bytes]:
    """OpenAI SSE chunks for one tool-call turn."""
    chunks = [_sse({"choices": [{"delta": {"role": "assistant", "tool_calls": [
        {
            "index": 0,
            "id": call_id,
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
        },
    ]}}]})]
    final: dict = {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
    if usage:
        final["usage"] = usage
    chunks.append(_sse(final))
    chunks.append(b"data: [DONE]\n\n")
    return chunks


_USAGE = {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}


def _payload_text(text: str) -> dict:
    return {
        "model": "e2e-model",
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": dict(_USAGE),
    }


def _payload_tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "model": "e2e-model",
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": dict(_USAGE),
    }


def _make_streaming_client(turns: list[list[bytes]]) -> ProviderClient:
    """OpenAI client whose HTTP transport serves one scripted SSE turn per call."""
    client = ProviderClient(OpenAIAdapter(LLMProviderConfig(
        provider="openai",
        api_key="k",
        base_url="https://llm.test/v1",
        model="e2e-model",
    )))
    calls = {"n": 0}

    async def send(request: httpx.Request, *, stream: bool = False) -> httpx.Response:
        chunks = turns[calls["n"]]
        calls["n"] += 1
        return httpx.Response(200, request=request, stream=_SseStream(chunks))

    client.adapter._http.send = send  # type: ignore[assignment]
    return client


def _make_non_streaming_client(turns: list[dict]) -> ProviderClient:
    client = ProviderClient(
        OpenAIAdapter(LLMProviderConfig(
            provider="openai",
            api_key="k",
            base_url="https://llm.test/v1",
            model="e2e-model",
        )),
        stream=False,
    )
    calls = {"n": 0}

    async def send(request: httpx.Request, *, stream: bool = False) -> httpx.Response:
        payload = turns[calls["n"]]
        calls["n"] += 1
        return httpx.Response(
            200,
            request=request,
            stream=_SseStream([json.dumps(payload).encode()]),
        )

    client.adapter._http.send = send  # type: ignore[assignment]
    return client


def _make_anthropic_client(turns: list[list[bytes]]) -> ProviderClient:
    client = ProviderClient(AnthropicAdapter(LLMProviderConfig(
        provider="anthropic",
        api_key="k",
        base_url="https://anthropic.test",
        model="claude-e2e",
        max_output_tokens=4096,
    )))
    calls = {"n": 0}

    async def send(request: httpx.Request, *, stream: bool = False) -> httpx.Response:
        chunks = turns[calls["n"]]
        calls["n"] += 1
        return httpx.Response(200, request=request, stream=_SseStream(chunks))

    client.adapter._http.send = send  # type: ignore[assignment]
    return client


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def greet(name: str) -> str:
        return f"Hello {name}"

    registry.register(
        "greet",
        "Greet someone by name.",
        {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        greet,
    )
    return registry


# ── driver ────────────────────────────────────────────────────────────────


def _run_loop(
    client,
    messages: list[dict],
    *,
    registry: ToolRegistry | None = None,
    max_iters: int = 5,
) -> tuple[list, list[GenerationRecord]]:
    """Run the canonical loop, collecting execution events and generation records."""
    events: list = []
    records: list[GenerationRecord] = []

    async def sink(record: GenerationRecord) -> None:
        records.append(record)

    async def run() -> None:
        with (
            generation_sink(sink),
            llm_purpose("main"),
            generation_context(run_id="e2e-run", session_id="e2e-session"),
        ):
            async for event in react_event_loop(
                client,
                messages,
                registry or _registry(),
                max_iters=max_iters,
            ):
                events.append(event)

    asyncio.run(run())
    return events, records


def _tool_results(events) -> list[dict]:
    return [event.data for event in events if event.type == EventType.TOOL_CALL_RESULT]


def _tool_starts(events) -> list[dict]:
    return [event.data for event in events if event.type == EventType.TOOL_CALL_START]


def _texts(events) -> list[str]:
    return [
        str(event.data.get("text", ""))
        for event in events
        if event.type == EventType.TEXT_DELTA
    ]


# ── streaming end-to-end ──────────────────────────────────────────────────


def test_streaming_loop_tool_call_and_final_text_end_to_end() -> None:
    client = _make_streaming_client([
        _chunks_tool_call("call-1", "greet", {"name": "world"}, usage=_USAGE),
        _chunks_text("Hello world", usage=_USAGE),
    ])

    events, records = _run_loop(client, [{"role": "user", "content": "Please greet the world"}])

    # The model turned into a real tool call that the registry executed.
    starts = _tool_starts(events)
    assert len(starts) == 1
    assert starts[0]["name"] == "greet"
    results = _tool_results(events)
    assert len(results) == 1
    assert results[0]["error"] is False
    assert results[0]["content"] == "Hello world"

    # The loop committed the final assistant text.
    assert "Hello world" in _texts(events)

    # Every model turn is accounted for with usage, correctly attributed.
    assert len(records) == 2
    assert all(record.ok for record in records)
    assert all(record.stream for record in records)
    assert all(record.purpose == "main" for record in records)
    assert all(record.run_id == "e2e-run" for record in records)
    assert all(record.session_id == "e2e-session" for record in records)
    assert all(record.usage["total_tokens"] > 0 for record in records)
    assert any(event.type == EventType.TOKEN_USAGE for event in events)


def test_non_streaming_loop_end_to_end() -> None:
    client = _make_non_streaming_client([
        _payload_tool_call("call-1", "greet", {"name": "world"}),
        _payload_text("Hello world"),
    ])

    events, records = _run_loop(client, [{"role": "user", "content": "Please greet the world"}])

    results = _tool_results(events)
    assert len(results) == 1
    assert results[0]["error"] is False
    assert results[0]["content"] == "Hello world"
    assert "Hello world" in _texts(events)

    assert len(records) == 2
    assert all(record.ok for record in records)
    assert all(not record.stream for record in records)
    assert all(record.usage["total_tokens"] > 0 for record in records)


# ── provider translation end-to-end ───────────────────────────────────────


def test_anthropic_streaming_loop_tool_and_final_text_end_to_end() -> None:
    """The Anthropic adapter translates the engine conversation and stream natively."""
    client = _make_anthropic_client([
        [
            b'event: message_start\ndata: {"type":"message_start","message":{}}\n\n',
            b'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
            b'"content_block":{"type":"tool_use","id":"toolu-1","name":"greet","input":{}}}\n\n',
            b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"input_json_delta","partial_json":"{\\"name\\":\\"world\\"}"}}\n\n',
            b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ],
        [
            b'event: message_start\ndata: {"type":"message_start","message":'
            b'{"usage":{"input_tokens":5}}}\n\n',
            b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":"Hello "}}\n\n',
            b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":"world"}}\n\n',
            b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
            b'"usage":{"output_tokens":7}}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ],
    ])

    events, records = _run_loop(client, [{"role": "user", "content": "Please greet the world"}])

    results = _tool_results(events)
    assert len(results) == 1
    assert results[0]["error"] is False
    assert results[0]["content"] == "Hello world"
    assert "Hello world" in _texts(events)

    assert len(records) == 2
    assert all(record.ok for record in records)
    assert all(record.stream for record in records)


# ── recovery end-to-end ───────────────────────────────────────────────────


def test_context_length_rejection_is_recovered_end_to_end() -> None:
    """A typed context-length rejection triggers compression and a retry."""
    client = _make_streaming_client([
        _chunks_tool_call("call-1", "greet", {"name": "world"}),
        _chunks_text("Hello world", usage=_USAGE),
    ])
    calls = {"n": 0}

    async def send(request: httpx.Request, *, stream: bool = False) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                400,
                request=request,
                stream=_SseStream([b'{"error":{"message":"This model\'s maximum context length is 128000 tokens"}}']),
            )
        chunks = [
            _chunks_tool_call("call-1", "greet", {"name": "world"}, usage=_USAGE),
            _chunks_text("Hello world", usage=_USAGE),
        ][calls["n"] - 2]
        return httpx.Response(200, request=request, stream=_SseStream(chunks))

    client.adapter._http.send = send  # type: ignore[assignment]

    events, records = _run_loop(client, [{"role": "user", "content": "Please greet the world"}])

    compression = [
        event
        for event in events
        if event.type in (EventType.CONTEXT_COMPRESSION_START, EventType.CONTEXT_COMPRESSION_END)
    ]
    assert len(compression) >= 2
    assert "Hello world" in _texts(events)

    # The rejected attempt is accounted as failed; both retried turns succeeded.
    assert len(records) == 3
    assert records[0].ok is False
    assert records[1].ok is True
    assert records[2].ok is True


# ── record & replay end-to-end ────────────────────────────────────────────


def test_record_then_replay_round_trip(tmp_path) -> None:
    path = tmp_path / "e2e.jsonl"
    recorder = RecordingLLM(
        _make_streaming_client([
            _chunks_tool_call("call-1", "greet", {"name": "world"}),
            _chunks_text("Hello world", usage=_USAGE),
        ]),
        path,
    )
    recorded_events, _ = _run_loop(
        recorder,
        [{"role": "user", "content": "Please greet the world"}],
    )

    turns = load_recording(path)
    assert len(turns) == 2
    assert turns[0].is_streaming

    replayed_events, _ = _run_loop(
        ReplayLLM(turns),
        [{"role": "user", "content": "Please greet the world"}],
    )

    # Same harness, same recorded model turns -> identical observable run shape.
    assert signature_diff(signature_of(recorded_events), signature_of(replayed_events)) == ""


# ── config resolution end-to-end ──────────────────────────────────────────


def test_resolve_build_and_call_end_to_end(tmp_path, monkeypatch) -> None:
    """resolve_llm_config -> build_llm_client -> chat, with a scripted transport."""
    import engine.llm.model_config as model_config

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.yaml").write_text(
        "llm:\n"
        "  api_key: test-key\n"
        "  base_url: https://llm.test/v1\n"
        "  model: e2e-model\n"
        "  max_output_tokens: 123\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(model_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(model_config, "SMITH_PROFILE_DIR", tmp_path / "smith")
    monkeypatch.setattr(model_config, "AGENT_DIR", tmp_path / "agent")

    resolved = model_config.resolve_llm_config()
    client = model_config.build_llm_client(resolved)
    captured: dict[str, object] = {}

    async def fake_send(request: httpx.Request, *, stream: bool = False) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            request=request,
            stream=_SseStream([json.dumps(_payload_text("hi")).encode()]),
        )

    try:
        client.adapter._http.send = fake_send  # type: ignore[assignment]
        response = asyncio.run(client.chat([{"role": "user", "content": "hello"}]))
    finally:
        asyncio.run(client.close())

    assert response.text == "hi"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "e2e-model"
    assert body["max_tokens"] == 123
