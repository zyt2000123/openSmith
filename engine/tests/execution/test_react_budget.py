from __future__ import annotations

import asyncio

from engine.execution.react.react_loop import (
    react_event_loop as _react_event_loop,
    react_loop as _react_loop,
    react_stream_loop as _react_stream_loop,
)
from engine.execution.events import EventType
from engine.execution.react.react_loop import FailedAgentRunError, IncompleteAgentRunError
from engine.llm.client import ChatResponse, ToolCallData
from engine.llm.contracts import LLMContextLengthError, LLMResponseError
from engine.llm.contracts import ModelLimits, ProviderCapabilities
from engine.llm.events import ProviderEvent, ProviderEventType
from engine.execution.react.budget import (
    CONVERSATION_HARD_LIMIT,
    CONVERSATION_KEEP_HEAD,
    CONVERSATION_KEEP_RECENT,
    MAX_FAILED_TOOL_RECOVERY_ITERS,
    MAX_IDENTICAL_TOOL_ERRORS,
    MAX_PREFLIGHT_CHALLENGE_ITERS,
)
from engine.safety.fact_gate import FactGate, FactGateContext
from engine.skill.executor import execute_skill
from engine.skill.loader import SkillBody, SkillMeta
from engine.tool.registry import ToolRegistry


class FakeLLM:
    def __init__(
        self,
        responses: list[ChatResponse],
        stream_chunks: list[str] | None = None,
    ) -> None:
        self.responses = list(responses)
        self.stream_chunks = list(stream_chunks or [])
        self.chat_calls: list[dict] = []
        self.stream_calls: list[list[dict]] = []

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        prefix_cache_key: str | None = None,
    ) -> ChatResponse:
        self.chat_calls.append({
            "messages": messages,
            "tools": tools,
            "prefix_cache_key": prefix_cache_key,
        })
        if not self.responses:
            return ChatResponse(text="final fallback")
        return self.responses.pop(0)

    async def chat_stream(
        self,
        messages: list[dict],
    ):
        self.stream_calls.append(messages)
        for chunk in self.stream_chunks:
            yield chunk


class StreamingFakeLLM(FakeLLM):
    stream = True

    def __init__(self, event_turns: list[list[ProviderEvent]]) -> None:
        super().__init__([])
        self.event_turns = list(event_turns)

    async def chat_events(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        prefix_cache_key: str | None = None,
    ):
        self.chat_calls.append({
            "messages": messages,
            "tools": tools,
            "prefix_cache_key": prefix_cache_key,
        })
        for event in self.event_turns.pop(0):
            yield event


def _tool_call(name: str = "fail", call_id: str = "call-1") -> ToolCallData:
    return ToolCallData(id=call_id, name=name, arguments={})


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def fail():
        return "Error: boom"

    async def fail_alt():
        return "Error: kaboom"

    async def ok():
        return "OK"

    registry.register("fail", "Always fails", {}, fail)
    registry.register("fail_alt", "Also fails", {}, fail_alt)
    registry.register("ok", "Succeeds", {}, ok)
    return registry


def test_react_loop_failed_tool_round_does_not_consume_main_budget():
    async def run():
        llm = FakeLLM([
            ChatResponse(tool_calls=[_tool_call()]),
            ChatResponse(text="recovered"),
        ])
        return await _react_loop(
            llm,
            [{"role": "user", "content": "try a tool"}],
            _registry(),
            max_iters=1,
        )

    assert asyncio.run(run()) == "recovered"


def test_react_event_loop_failed_tool_round_can_still_stream_final_text():
    async def run():
        llm = FakeLLM(
            [
                ChatResponse(tool_calls=[_tool_call()]),
                ChatResponse(text="recovered"),
            ],
            stream_chunks=["recovered"],
        )
        events = []
        async for event in _react_event_loop(
            llm,
            [{"role": "user", "content": "try a tool"}],
            _registry(),
            max_iters=1,
        ):
            events.append(event)
        return events

    events = asyncio.run(run())
    text = "".join(
        event.data.get("text", "")
        for event in events
        if event.type == EventType.TEXT_DELTA
    )
    assert text == "recovered"


def test_react_event_loop_forwards_provider_text_deltas_without_duplicate_final_text() -> None:
    async def run():
        llm = StreamingFakeLLM([[
            ProviderEvent(ProviderEventType.RESPONSE_CREATED),
            ProviderEvent(ProviderEventType.OUTPUT_TEXT_DELTA, {"delta": "live "}),
            ProviderEvent(ProviderEventType.OUTPUT_TEXT_DELTA, {"delta": "reply"}),
            ProviderEvent(
                ProviderEventType.RESPONSE_COMPLETED,
                {"finish_reason": "stop", "raw_finish_reason": "stop"},
            ),
        ]])
        events = []
        async for event in _react_event_loop(
            llm,
            [{"role": "user", "content": "hello"}],
            _registry(),
        ):
            events.append(event)
        return events

    events = asyncio.run(run())
    raw_text = "".join(
        event.data.get("data", {}).get("delta", "")
        for event in events
        if event.type == EventType.RAW_RESPONSE_EVENT
    )
    final = [event for event in events if event.type == EventType.TEXT_DELTA]

    assert raw_text == "live reply"
    assert len(final) == 1
    assert final[0].data == {"text": "live reply", "already_streamed": True}


def test_react_event_loop_retracts_streamed_draft_before_repairing_final_answer() -> None:
    async def run():
        llm = StreamingFakeLLM([
            [
                ProviderEvent(
                    ProviderEventType.FUNCTION_CALL_ARGUMENTS_DELTA,
                    {"index": 0, "id": "tool-1", "name": "ok", "arguments_delta": "{}"},
                ),
                ProviderEvent(ProviderEventType.RESPONSE_COMPLETED, {"finish_reason": "stop"}),
            ],
            [
                ProviderEvent(ProviderEventType.OUTPUT_TEXT_DELTA, {"delta": "让我再查一下。"}),
                ProviderEvent(ProviderEventType.RESPONSE_COMPLETED, {"finish_reason": "stop"}),
            ],
            [
                ProviderEvent(ProviderEventType.OUTPUT_TEXT_DELTA, {"delta": "最终答案。"}),
                ProviderEvent(ProviderEventType.RESPONSE_COMPLETED, {"finish_reason": "stop"}),
            ],
        ])
        return [
            event
            async for event in _react_event_loop(
                llm,
                [{"role": "user", "content": "answer with evidence"}],
                _registry(),
                max_iters=2,
            )
        ]

    events = asyncio.run(run())
    drafts = [event.data for event in events if event.type == EventType.PROVISIONAL_TEXT_DELTA]
    retractions = [event.data for event in events if event.type == EventType.PROVISIONAL_RETRACT]
    commits = [event.data for event in events if event.type == EventType.PROVISIONAL_COMMIT]
    finals = [event.data for event in events if event.type == EventType.TEXT_DELTA]

    assert [draft["text"] for draft in drafts] == ["让我再查一下。", "最终答案。"]
    assert retractions == [{"provision_id": drafts[0]["provision_id"], "reason": "incomplete_final_repair"}]
    assert commits == [{"provision_id": drafts[1]["provision_id"]}]
    assert finals == [{"text": "最终答案。", "already_streamed": True}]


def test_react_stream_loop_hides_retracted_provisional_draft() -> None:
    async def run() -> list[str]:
        llm = StreamingFakeLLM([
            [
                ProviderEvent(
                    ProviderEventType.FUNCTION_CALL_ARGUMENTS_DELTA,
                    {"index": 0, "id": "tool-1", "name": "ok", "arguments_delta": "{}"},
                ),
                ProviderEvent(ProviderEventType.RESPONSE_COMPLETED, {"finish_reason": "stop"}),
            ],
            [
                ProviderEvent(ProviderEventType.OUTPUT_TEXT_DELTA, {"delta": "让我再查一下。"}),
                ProviderEvent(ProviderEventType.RESPONSE_COMPLETED, {"finish_reason": "stop"}),
            ],
            [
                ProviderEvent(ProviderEventType.OUTPUT_TEXT_DELTA, {"delta": "最终答案。"}),
                ProviderEvent(ProviderEventType.RESPONSE_COMPLETED, {"finish_reason": "stop"}),
            ],
        ])
        return [
            chunk
            async for chunk in _react_stream_loop(
                llm,
                [{"role": "user", "content": "answer with evidence"}],
                _registry(),
                max_iters=2,
            )
        ]

    assert asyncio.run(run()) == ["最终答案。"]


def test_react_event_loop_never_executes_a_length_truncated_tool_call() -> None:
    async def run():
        llm = StreamingFakeLLM([[
            ProviderEvent(ProviderEventType.RESPONSE_CREATED),
            ProviderEvent(
                ProviderEventType.FUNCTION_CALL_ARGUMENTS_DELTA,
                {
                    "index": 0,
                    "id": "call-1",
                    "name": "ok",
                    "arguments_delta": '{"path":"partial',
                },
            ),
            ProviderEvent(
                ProviderEventType.RESPONSE_COMPLETED,
                {"finish_reason": "length", "raw_finish_reason": "length"},
            ),
        ]])
        events = []
        async for event in _react_event_loop(
            llm,
            [{"role": "user", "content": "read a file"}],
            _registry(),
        ):
            events.append(event)
        return events

    events = asyncio.run(run())
    incomplete = [event for event in events if event.type == EventType.INCOMPLETE]

    assert not any(event.type == EventType.TOOL_CALL_START for event in events)
    assert incomplete[0].data == {
        "reason": "model_output_limit",
        "phase": "tool_call",
        "continuations": 0,
    }


def test_react_event_loop_returns_preflight_to_model_without_executing_or_failing():
    async def run():
        executions: list[str] = []
        registry = ToolRegistry()

        async def edit_file(path: str):
            executions.append(path)
            return "edited"

        registry.register("edit_file", "Edit a file", {}, edit_file)
        first_call = ToolCallData(
            id="edit-1",
            name="edit_file",
            arguments={"path": "engine/example.py"},
        )
        same_round_call = ToolCallData(
            id="edit-2",
            name="edit_file",
            arguments={"path": "engine/example.py"},
        )
        retry_call = ToolCallData(
            id="edit-3",
            name="edit_file",
            arguments={"path": "engine/example.py"},
        )
        llm = FakeLLM([
            ChatResponse(tool_calls=[first_call, same_round_call]),
            ChatResponse(tool_calls=[retry_call]),
            ChatResponse(text="done"),
        ])
        gate = FactGate(FactGateContext("session-1", "turn-1"))
        events = []
        async for event in _react_event_loop(
            llm,
            [{"role": "user", "content": "edit the file"}],
            registry,
            max_iters=2,
            fact_gate=gate,
        ):
            events.append(event)
        return events, llm, executions

    events, llm, executions = asyncio.run(run())

    results = [event for event in events if event.type == EventType.TOOL_CALL_RESULT]
    assert results[0].data["preflight"] is True
    assert results[0].data["blocked"] is False
    assert results[0].data["error"] is False
    assert results[1].data["preflight"] is True
    assert results[2].data["preflight"] is False
    assert executions == ["engine/example.py"]
    assert any(
        message.get("role") == "tool" and str(message.get("content", "")).startswith("[PREFLIGHT]")
        for message in llm.chat_calls[1]["messages"]
    )
    assert not any(
        message.get("role") == "system" and "failed consecutively" in str(message.get("content", ""))
        for call in llm.chat_calls
        for message in call["messages"]
    )


def test_preflight_budget_counts_rounds_that_also_have_successful_tools() -> None:
    async def run() -> str:
        registry = ToolRegistry()

        async def read_file(path: str):
            return f"read {path}"

        async def write_file(path: str):
            return f"wrote {path}"

        registry.register("read_file", "Read", {}, read_file)
        registry.register("write_file", "Write", {}, write_file)
        responses = []
        for index in range(MAX_PREFLIGHT_CHALLENGE_ITERS):
            responses.append(ChatResponse(tool_calls=[
                ToolCallData(id=f"read-{index}", name="read_file", arguments={"path": f"input-{index}.txt"}),
                ToolCallData(id=f"write-{index}", name="write_file", arguments={"path": f"output-{index}.txt"}),
            ]))
        llm = FakeLLM(responses)
        gate = FactGate(FactGateContext("session-1", "turn-1"))
        return await _react_loop(
            llm,
            [{"role": "user", "content": "change many files"}],
            registry,
            max_iters=MAX_PREFLIGHT_CHALLENGE_ITERS + 5,
            fact_gate=gate,
        )

    try:
        asyncio.run(run())
        assert False, "should have raised IncompleteAgentRunError"
    except IncompleteAgentRunError as exc:
        assert exc.reason == "preflight_budget"


def test_react_event_loop_uses_decision_response_as_final_text():
    async def run():
        llm = FakeLLM(
            [ChatResponse(text="decision final")],
            stream_chunks=["different stream text"],
        )
        events = []
        async for event in _react_event_loop(
            llm,
            [{"role": "user", "content": "answer directly"}],
            _registry(),
            max_iters=1,
        ):
            events.append(event)
        return events, llm

    events, llm = asyncio.run(run())
    text = "".join(
        event.data.get("text", "")
        for event in events
        if event.type == EventType.TEXT_DELTA
    )
    assert text == "decision final"
    assert llm.stream_calls == []


def test_react_event_loop_continues_after_model_length_finish_reason():
    async def run():
        llm = FakeLLM([
            ChatResponse(text="first half ", finish_reason="length"),
            ChatResponse(text="second half", finish_reason="stop"),
        ])
        events = []
        async for event in _react_event_loop(
            llm,
            [{"role": "user", "content": "answer completely"}],
            _registry(),
            max_iters=1,
        ):
            events.append(event)
        return events, llm

    events, llm = asyncio.run(run())
    text = "".join(
        event.data.get("text", "")
        for event in events
        if event.type == EventType.TEXT_DELTA
    )

    assert text == "first half second half"
    assert len(llm.chat_calls) == 2
    assert llm.chat_calls[1]["messages"][-2] == {
        "role": "assistant",
        "content": "first half ",
    }
    assert "cut off" in llm.chat_calls[1]["messages"][-1]["content"]


def test_react_event_loop_discards_length_draft_when_continuation_calls_tool():
    async def run():
        llm = StreamingFakeLLM([
            [
                ProviderEvent(ProviderEventType.OUTPUT_TEXT_DELTA, {"delta": "partial "}),
                ProviderEvent(
                    ProviderEventType.RESPONSE_COMPLETED,
                    {"finish_reason": "length", "raw_finish_reason": "length"},
                ),
            ],
            [
                ProviderEvent(
                    ProviderEventType.FUNCTION_CALL_ARGUMENTS_DELTA,
                    {"index": 0, "id": "tool-1", "name": "ok", "arguments_delta": "{}"},
                ),
                ProviderEvent(
                    ProviderEventType.RESPONSE_COMPLETED,
                    {"finish_reason": "tool_calls", "raw_finish_reason": "tool_calls"},
                ),
            ],
            [
                ProviderEvent(ProviderEventType.OUTPUT_TEXT_DELTA, {"delta": "answer"}),
                ProviderEvent(
                    ProviderEventType.RESPONSE_COMPLETED,
                    {"finish_reason": "stop", "raw_finish_reason": "stop"},
                ),
            ],
        ])
        return [
            event
            async for event in _react_event_loop(
                llm,
                [{"role": "user", "content": "answer completely"}],
                _registry(),
                max_iters=2,
            )
        ]

    events = asyncio.run(run())
    drafts = [event.data for event in events if event.type == EventType.PROVISIONAL_TEXT_DELTA]
    retractions = [event.data for event in events if event.type == EventType.PROVISIONAL_RETRACT]
    commits = [event.data for event in events if event.type == EventType.PROVISIONAL_COMMIT]
    finals = [event.data for event in events if event.type == EventType.TEXT_DELTA]

    assert [draft["text"] for draft in drafts] == ["partial ", "answer"]
    assert retractions == [
        {"provision_id": drafts[0]["provision_id"], "reason": "tool_call_pending"},
    ]
    assert commits == [{"provision_id": drafts[1]["provision_id"]}]
    assert finals == [{"text": "answer", "already_streamed": True}]


def test_react_event_loop_uses_deterministic_recovery_when_compaction_fails():
    class CompressionFailingStreamingLLM(StreamingFakeLLM):
        context_window = 10_000

        def __init__(self) -> None:
            super().__init__([
                [
                    ProviderEvent(ProviderEventType.OUTPUT_TEXT_DELTA, {"delta": "x" * 24_000}),
                    ProviderEvent(
                        ProviderEventType.RESPONSE_COMPLETED,
                        {"finish_reason": "length", "raw_finish_reason": "length"},
                    ),
                ],
                [
                    ProviderEvent(
                        ProviderEventType.OUTPUT_TEXT_DELTA,
                        {"delta": "continued"},
                    ),
                    ProviderEvent(
                        ProviderEventType.RESPONSE_COMPLETED,
                        {"finish_reason": "stop", "raw_finish_reason": "stop"},
                    ),
                ],
            ])
            self.compactor_calls = 0

        async def chat(self, messages, tools=None, prefix_cache_key=None):
            self.compactor_calls += 1
            raise RuntimeError("compactor unavailable")

    async def run():
        llm = CompressionFailingStreamingLLM()
        events = [
            event
            async for event in _react_event_loop(
                llm,
                [{"role": "user", "content": "answer completely"}],
                _registry(),
            )
        ]
        return events, llm

    events, llm = asyncio.run(run())
    context = [
        event.data
        for event in events
        if event.type == EventType.CONTEXT_USAGE
    ]

    assert llm.compactor_calls == 1
    assert any(item["fit_status"] == "recovered" for item in context)
    assert not [
        event for event in events if event.type in {EventType.INCOMPLETE, EventType.FAILED}
    ]
    assert [
        event.data["text"]
        for event in events
        if event.type == EventType.TEXT_DELTA
    ][-1].endswith("continued")


def test_react_event_loop_marks_repeated_model_length_as_incomplete():
    async def run():
        llm = FakeLLM([
            ChatResponse(text="part-1 ", finish_reason="length"),
            ChatResponse(text="part-2 ", finish_reason="length"),
            ChatResponse(text="part-3", finish_reason="length"),
        ])
        events = []
        async for event in _react_event_loop(
            llm,
            [{"role": "user", "content": "answer completely"}],
            _registry(),
            max_iters=1,
        ):
            events.append(event)
        return events

    events = asyncio.run(run())
    text = "".join(
        event.data.get("text", "")
        for event in events
        if event.type == EventType.TEXT_DELTA
    )
    incomplete = [event for event in events if event.type == EventType.INCOMPLETE]

    assert text == "part-1 part-2 part-3"
    assert len(incomplete) == 1
    assert incomplete[0].data == {"reason": "model_output_limit", "continuations": 2}


def test_react_event_loop_recovers_once_from_context_limit_error():
    class ContextLimitedLLM(FakeLLM):
        def __init__(self) -> None:
            super().__init__(responses=[])
            self.calls = 0

        async def chat(self, messages, tools=None, prefix_cache_key=None):
            self.calls += 1
            if self.calls == 1:
                raise LLMResponseError("HTTP 400: context_length_exceeded")
            return ChatResponse(text="recovered")

    async def run():
        llm = ContextLimitedLLM()
        events = [
            event
            async for event in _react_event_loop(
                llm,
                [{"role": "user", "content": "hello"}],
                _registry(),
            )
        ]
        return events, llm

    events, llm = asyncio.run(run())

    assert llm.calls == 2
    assert any(event.type == EventType.CONTEXT_COMPRESSION_START for event in events)
    assert any(event.type == EventType.CONTEXT_COMPRESSION_END for event in events)
    assert [event.data["text"] for event in events if event.type == EventType.TEXT_DELTA] == ["recovered"]
    assert not [event for event in events if event.type == EventType.INCOMPLETE]


def test_stream_context_rejection_recovers_without_replaying_as_non_stream():
    class ContextRejectedStreamLLM(FakeLLM):
        stream = True

        def __init__(self) -> None:
            super().__init__([])
            self.stream_attempts = 0
            self.fallback_calls = 0

        async def chat_events(self, messages, tools=None):
            self.stream_attempts += 1
            if self.stream_attempts == 1:
                raise LLMContextLengthError(
                    "request exceeds context window",
                    http_status=400,
                    provider_code="context_length_exceeded",
                )
            yield ProviderEvent(
                ProviderEventType.OUTPUT_TEXT_DELTA,
                {"delta": "recovered"},
            )
            yield ProviderEvent(
                ProviderEventType.RESPONSE_COMPLETED,
                {"finish_reason": "stop", "raw_finish_reason": "stop"},
            )

        async def chat(self, messages, tools=None, prefix_cache_key=None):
            self.fallback_calls += 1
            return ChatResponse(text="must not replay")

    async def run():
        llm = ContextRejectedStreamLLM()
        events = [
            event
            async for event in _react_event_loop(
                llm,
                [{"role": "user", "content": "hello"}],
                _registry(),
            )
        ]
        return events, llm

    events, llm = asyncio.run(run())

    assert llm.stream_attempts == 2
    assert llm.fallback_calls == 0
    assert [event.data["text"] for event in events if event.type == EventType.TEXT_DELTA] == [
        "recovered"
    ]


def test_react_loop_collects_decision_response_from_canonical_events():
    async def run():
        llm = FakeLLM(
            [ChatResponse(text="decision final")],
            stream_chunks=["different stream text"],
        )
        output = await _react_loop(
            llm,
            [{"role": "user", "content": "answer directly"}],
            _registry(),
            max_iters=1,
        )
        return output, llm

    output, llm = asyncio.run(run())
    assert output == "decision final"
    assert llm.stream_calls == []


def test_react_stream_loop_collects_decision_response_from_canonical_events():
    async def run():
        llm = FakeLLM(
            [ChatResponse(text="stream decision")],
            stream_chunks=["different stream text"],
        )
        chunks: list[str] = []
        async for chunk in _react_stream_loop(
            llm,
            [{"role": "user", "content": "answer directly"}],
            _registry(),
            max_iters=1,
        ):
            chunks.append(chunk)
        return chunks, llm

    chunks, llm = asyncio.run(run())
    assert chunks == ["stream decision"]
    assert llm.stream_calls == []


def test_react_event_loop_repairs_incomplete_final_after_tool_success():
    async def run():
        llm = FakeLLM([
            ChatResponse(text="Searching first.", tool_calls=[_tool_call("ok", "search-1")]),
            ChatResponse(text="让我抓取一个排行榜页面获取更详细的信息。"),
            ChatResponse(text="Fetching details.", tool_calls=[_tool_call("ok", "fetch-1")]),
            ChatResponse(text="最终答案：目前没有单一绝对最好的大模型，需要按场景比较。"),
        ])
        events = []
        async for event in _react_event_loop(
            llm,
            [{"role": "user", "content": "现在最好的大语言模型是哪个？"}],
            _registry(),
            max_iters=5,
        ):
            events.append(event)
        return events, llm

    events, llm = asyncio.run(run())
    tool_starts = [
        event
        for event in events
        if event.type == EventType.TOOL_CALL_START
    ]
    text = "".join(
        event.data.get("text", "")
        for event in events
        if event.type == EventType.TEXT_DELTA
    )

    assert [event.data["id"] for event in tool_starts] == ["search-1", "fetch-1"]
    assert "最终答案" in text
    assert len(llm.chat_calls) == 4


def test_react_event_loop_emits_token_usage():
    async def run():
        llm = FakeLLM(
            [
                ChatResponse(
                    text="done",
                    usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
                ),
            ],
            stream_chunks=["done"],
        )
        events = []
        async for event in _react_event_loop(
            llm,
            [{"role": "user", "content": "hello"}],
            _registry(),
            max_iters=1,
        ):
            events.append(event)
        return events

    events = asyncio.run(run())
    usage_events = [event for event in events if event.type == EventType.TOKEN_USAGE]
    assert len(usage_events) == 1
    assert usage_events[0].data == {
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
        # Flags that the provider reported usage; not forwarded over SSE.
        "usage_reported": 1,
    }
    context_events = [event for event in events if event.type == EventType.CONTEXT_USAGE]
    context = context_events[-1].data
    assert context["context_tokens"] == 11
    assert context["context_window"] == 128_000
    assert context["effective_context_window"] == 128_000
    assert context["safe_input_budget"] < context["context_window"]
    assert context["output_reserve"] == 4_096
    assert context["tool_schema_tokens"] > 0
    assert context["context_percent"] == 0
    assert context["estimated"] is False
    assert context["fit_status"] == "fit"


def test_react_event_loop_compacts_large_conversation_before_answering():
    async def run():
        llm = FakeLLM(
            [
                ChatResponse(text="compact summary"),
                ChatResponse(text="done"),
            ],
            stream_chunks=["done"],
        )
        events = []
        async for event in _react_event_loop(
            llm,
            [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "x" * 400_000},
            ],
            _registry(),
            max_iters=1,
        ):
            events.append(event)
        return events, llm

    events, llm = asyncio.run(run())

    assert len(llm.chat_calls) >= 2
    assert "Summarize our conversation above" in llm.chat_calls[0]["messages"][-1]["content"]
    assert [event.type for event in events].count(EventType.CONTEXT_COMPRESSION_START) == 1
    assert [event.type for event in events].count(EventType.CONTEXT_COMPRESSION_END) == 1
    text = "".join(
        event.data.get("text", "")
        for event in events
        if event.type == EventType.TEXT_DELTA
    )
    assert text == "done"


def test_react_event_loop_rejects_unfit_tool_schemas_before_provider_call():
    class SmallContextLLM(FakeLLM):
        limits = ModelLimits(
            context_window=4_096,
            context_window_declared=True,
            max_output_tokens=1_024,
            max_output_tokens_declared=True,
        )

    async def run():
        registry = ToolRegistry()

        async def oversized():
            return "unreachable"

        registry.register(
            "oversized",
            "证" * 4_000,
            {"type": "object", "properties": {}},
            oversized,
        )
        llm = SmallContextLLM([ChatResponse(text="must not be called")])
        events = [
            event
            async for event in _react_event_loop(
                llm,
                [{"role": "user", "content": "hello"}],
                registry,
                max_iters=1,
            )
        ]
        return events, llm

    events, llm = asyncio.run(run())
    incomplete = [event for event in events if event.type == EventType.INCOMPLETE]

    assert llm.chat_calls == []
    assert len(incomplete) == 1
    assert incomplete[0].data["reason"] == "context_capacity_exhausted"
    assert incomplete[0].data["fit_status"] == "unfit_tool_schemas"


def test_react_event_loop_forwards_prefix_cache_key_to_capable_provider():
    class PrefixCapableLLM(FakeLLM):
        capabilities = ProviderCapabilities(prefix_cache_key=True)

    async def run():
        llm = PrefixCapableLLM([ChatResponse(text="done")])
        events = [
            event
            async for event in _react_event_loop(
                llm,
                [{"role": "user", "content": "hello"}],
                _registry(),
                max_iters=1,
                prefix_cache_key="stable-prefix",
            )
        ]
        return events, llm

    events, llm = asyncio.run(run())

    assert any(event.type == EventType.TEXT_DELTA for event in events)
    assert llm.chat_calls[0]["prefix_cache_key"] == "stable-prefix"


def test_execute_skill_failed_tool_round_does_not_consume_main_budget():
    async def run():
        skill = SkillBody(meta=SkillMeta(name="sample"), content="Use tools if needed.")
        llm = FakeLLM([
            ChatResponse(tool_calls=[_tool_call()]),
            ChatResponse(text="skill recovered"),
        ])
        return await execute_skill(
            skill,
            llm,
            _registry(),
            [{"role": "user", "content": "try a tool"}],
            {"user_message": "try a tool"},
            max_iters=1,
            react_loop_fn=_react_loop,
        )

    assert asyncio.run(run()) == "skill recovered"


def test_react_loop_failed_tool_recovery_budget_forces_text_finalization():
    async def run():
        failures = [
            ChatResponse(tool_calls=[
                _tool_call(name="fail" if idx % 2 == 0 else "fail_alt", call_id=f"call-{idx}")
            ])
            for idx in range(MAX_FAILED_TOOL_RECOVERY_ITERS)
        ]
        llm = FakeLLM([*failures, ChatResponse(text="unused no-tool final")])
        await _react_loop(
            llm,
            [{"role": "user", "content": "keep trying"}],
            _registry(),
            max_iters=1,
        )

    try:
        asyncio.run(run())
        assert False, "should have raised IncompleteAgentRunError"
    except IncompleteAgentRunError as exc:
        assert exc.reason == "tool_failure_budget"


# ---------------------------------------------------------------------------
# P0 regression: text adapters must propagate all terminal states
# ---------------------------------------------------------------------------

def test_react_event_loop_marks_empty_final_after_successful_tool_incomplete():
    async def run():
        llm = FakeLLM([
            ChatResponse(tool_calls=[_tool_call("ok")]),
            ChatResponse(text=""),
        ])
        return [
            event
            async for event in _react_event_loop(
                llm,
                [{"role": "user", "content": "use a tool"}],
                _registry(),
                max_iters=2,
            )
        ]

    events = asyncio.run(run())

    assert [event.data for event in events if event.type == EventType.INCOMPLETE] == [
        {"reason": "empty_model_response"},
    ]


def test_react_loop_raises_on_empty_final_after_successful_tool():
    async def run():
        llm = FakeLLM([
            ChatResponse(tool_calls=[_tool_call("ok")]),
            ChatResponse(text=""),
        ])
        return await _react_loop(
            llm,
            [{"role": "user", "content": "use a tool"}],
            _registry(),
            max_iters=2,
        )

    try:
        asyncio.run(run())
        assert False, "should have raised"
    except IncompleteAgentRunError as exc:
        assert exc.reason == "empty_model_response"


def test_react_loop_raises_on_content_filter():
    """content_filter INCOMPLETE must not be silently swallowed."""
    async def run():
        llm = FakeLLM([ChatResponse(text="partial", finish_reason="content_filter")])
        return await _react_loop(
            llm,
            [{"role": "user", "content": "hi"}],
            _registry(),
        )

    try:
        asyncio.run(run())
        assert False, "should have raised"
    except IncompleteAgentRunError as exc:
        assert exc.reason == "content_filter"


def test_react_loop_raises_on_tool_failure_budget():
    """tool_failure_budget INCOMPLETE must raise, not return partial text."""
    async def run():
        failures = [
            ChatResponse(tool_calls=[
                _tool_call(name="fail" if i % 2 == 0 else "fail_alt", call_id=f"call-{i}")
            ])
            for i in range(MAX_FAILED_TOOL_RECOVERY_ITERS)
        ]
        llm = FakeLLM([*failures, ChatResponse(text="unreachable")])
        return await _react_loop(
            llm,
            [{"role": "user", "content": "try"}],
            _registry(),
            max_iters=1,
        )

    try:
        asyncio.run(run())
        assert False, "should have raised"
    except IncompleteAgentRunError as exc:
        assert exc.reason == "tool_failure_budget"


def test_react_loop_raises_on_provider_failure():
    """FAILED event (provider_finish_error) must raise FailedAgentRunError."""
    async def run():
        llm = FakeLLM([ChatResponse(text="oops", finish_reason="error")])
        return await _react_loop(
            llm,
            [{"role": "user", "content": "hi"}],
            _registry(),
        )

    try:
        asyncio.run(run())
        assert False, "should have raised"
    except FailedAgentRunError as exc:
        assert exc.reason == "provider_finish_error"


def test_react_stream_loop_raises_on_content_filter():
    """Stream adapter must also propagate INCOMPLETE."""
    async def run():
        llm = FakeLLM([ChatResponse(text="partial", finish_reason="content_filter")])
        chunks = []
        async for chunk in _react_stream_loop(
            llm,
            [{"role": "user", "content": "hi"}],
            _registry(),
        ):
            chunks.append(chunk)
        return chunks

    try:
        asyncio.run(run())
        assert False, "should have raised"
    except IncompleteAgentRunError as exc:
        assert exc.reason == "content_filter"


def test_react_event_loop_identical_tool_error_short_circuits():
    """Repeated identical tool errors should exit before recovery budget."""
    async def run():
        failures = [
            ChatResponse(tool_calls=[_tool_call(call_id=f"call-{i}")])
            for i in range(MAX_IDENTICAL_TOOL_ERRORS)
        ]
        llm = FakeLLM([*failures, ChatResponse(text="unreachable")])
        return [
            event
            async for event in _react_event_loop(
                llm,
                [{"role": "user", "content": "try"}],
                _registry(),
                max_iters=MAX_IDENTICAL_TOOL_ERRORS + 5,
            )
        ]

    events = asyncio.run(run())
    incomplete = [e for e in events if e.type == EventType.INCOMPLETE]
    assert len(incomplete) == 1
    assert incomplete[0].data["reason"] == "identical_tool_error_loop"


def test_react_event_loop_conversation_pruning_keeps_head():
    """Pruning must keep system + first user message, not just conversation[0]."""
    async def run():
        base = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "important initial question"},
        ]
        padding = []
        for i in range(CONVERSATION_HARD_LIMIT):
            padding.append({"role": "assistant", "content": f"reply-{i}"})
            padding.append({"role": "user", "content": f"follow-up-{i}"})
        llm = FakeLLM([ChatResponse(text="final")])
        events = []
        async for event in _react_event_loop(
            llm,
            base + padding,
            _registry(),
            max_iters=1,
        ):
            events.append(event)
        return llm.chat_calls[0]["messages"]

    messages = asyncio.run(run())
    assert messages[0] == {"role": "system", "content": "system prompt"}
    assert messages[1] == {"role": "user", "content": "important initial question"}
    assert len(messages) == CONVERSATION_KEEP_HEAD + CONVERSATION_KEEP_RECENT


def test_react_event_loop_stream_fallback_on_early_error():
    """If streaming fails before any content, fall back to llm.chat()."""
    async def run():
        class FailStreamLLM(FakeLLM):
            stream = True

            async def chat_events(self, messages, tools=None):
                raise ConnectionError("stream died")
                yield  # make it an async generator

        llm = FailStreamLLM([ChatResponse(text="fallback result")])
        return [
            event
            async for event in _react_event_loop(
                llm,
                [{"role": "user", "content": "hello"}],
                _registry(),
            )
        ]

    events = asyncio.run(run())
    text = "".join(
        e.data.get("text", "")
        for e in events
        if e.type == EventType.TEXT_DELTA
    )
    assert text == "fallback result"


def test_react_event_loop_does_not_replay_after_reasoning_only_stream_error():
    """A partially consumed reasoning stream is not safe to replay."""
    async def run() -> tuple[bool, int]:
        class ReasoningThenDisconnectLLM(FakeLLM):
            stream = True

            def __init__(self) -> None:
                super().__init__([ChatResponse(text="must not be replayed")])
                self.fallback_calls = 0

            async def chat_events(self, messages, tools=None):
                yield ProviderEvent(
                    ProviderEventType.REASONING_DELTA,
                    {"delta": "checking the available tools"},
                )
                raise ConnectionError("stream died after reasoning")

            async def chat(self, messages, tools=None, prefix_cache_key=None):
                self.fallback_calls += 1
                return await super().chat(messages, tools, prefix_cache_key)

        llm = ReasoningThenDisconnectLLM()
        try:
            async for _event in _react_event_loop(
                llm,
                [{"role": "user", "content": "hello"}],
                _registry(),
            ):
                pass
        except ConnectionError:
            return True, llm.fallback_calls
        return False, llm.fallback_calls

    interrupted, fallback_calls = asyncio.run(run())

    assert interrupted is True
    assert fallback_calls == 0


def _assert_tool_pairing_intact(messages: list[dict]) -> None:
    """Provider-400 invariant: every tool result answers an open call from the
    immediately preceding assistant turn, and no call goes unanswered."""
    open_ids: set[str] = set()
    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            assert not open_ids, f"unanswered tool calls before assistant: {open_ids}"
            for tc in msg.get("tool_calls") or []:
                open_ids.add(tc["id"])
        elif role == "user":
            assert not open_ids, f"user turn with pending tool calls: {open_ids}"
        elif role == "tool":
            call_id = msg.get("tool_call_id")
            assert call_id in open_ids, f"orphan tool result: {call_id!r}"
            open_ids.discard(call_id)
    assert not open_ids, f"conversation ends with unanswered tool calls: {open_ids}"


def _tool_call_entry(call_id: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "ok", "arguments": "{}"},
    }


def test_hard_limit_cut_inside_tool_round_keeps_pairing():
    """R8 回归：切点落在 tool 结果串（含 system 提示交错）内必须回退到轮次边界。"""
    async def run():
        conversation = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "question"},
        ]
        for i in range(7):  # indices 2..8
            conversation.append({
                "role": "assistant" if i % 2 == 0 else "user",
                "content": f"pad-{i}",
            })
        conversation.append({  # index 9
            "role": "assistant",
            "content": "",
            "tool_calls": [
                _tool_call_entry("call-a"),
                _tool_call_entry("call-b"),
                _tool_call_entry("call-c"),
            ],
        })
        conversation.append({"role": "tool", "tool_call_id": "call-a", "content": "result-a"})
        conversation.append({"role": "system", "content": "recovery hint"})
        conversation.append({"role": "tool", "tool_call_id": "call-b", "content": "result-b"})
        conversation.append({"role": "tool", "tool_call_id": "call-c", "content": "result-c"})
        for i in range(27):  # indices 14..40 → 41 messages total
            conversation.append({
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"tail-{i}",
            })
        assert len(conversation) > CONVERSATION_HARD_LIMIT
        raw_cut = len(conversation) - CONVERSATION_KEEP_RECENT
        # 前置条件：天然切点恰好落在 tool 结果上，逼出边界回退。
        assert conversation[raw_cut]["role"] == "tool"

        llm = FakeLLM([ChatResponse(text="final")])
        async for _ in _react_event_loop(llm, conversation, _registry(), max_iters=1):
            pass
        return llm.chat_calls[0]["messages"]

    messages = asyncio.run(run())
    _assert_tool_pairing_intact(messages)
    assert messages[0]["content"] == "system prompt"
    assert messages[1]["content"] == "question"
    assert len(messages) < CONVERSATION_HARD_LIMIT


def test_hard_limit_prefers_valid_pairing_over_truncation():
    """一整轮巨型 tool 串无法安全切分时，保留完整对话而不是切出孤儿。"""
    async def run():
        tool_count = CONVERSATION_HARD_LIMIT  # one assistant turn with 40 calls
        conversation = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "question"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [_tool_call_entry(f"call-{i}") for i in range(tool_count)],
            },
        ]
        for i in range(tool_count):
            conversation.append({
                "role": "tool",
                "tool_call_id": f"call-{i}",
                "content": f"result-{i}",
            })
        assert len(conversation) > CONVERSATION_HARD_LIMIT

        llm = FakeLLM([ChatResponse(text="final")])
        async for _ in _react_event_loop(llm, conversation, _registry(), max_iters=1):
            pass
        return llm.chat_calls[0]["messages"], len(conversation)

    messages, original_len = asyncio.run(run())
    _assert_tool_pairing_intact(messages)
    assert len(messages) == original_len


def test_incomplete_final_detects_chinese_look_verbs():
    from engine.execution.react.budget import looks_like_incomplete_final_after_tool

    # 看看/看一下 belong to the Chinese verb set (regression: they were
    # sliced into the English pattern and never matched).
    assert looks_like_incomplete_final_after_tool("好的，让我看一下相关文件。")
    assert looks_like_incomplete_final_after_tool("接下来看看测试结果。")
    assert looks_like_incomplete_final_after_tool("Let me check the config file.")
    assert not looks_like_incomplete_final_after_tool("修复完成，所有测试通过。")


def test_react_event_loop_injects_engine_control_after_a_blocked_tool() -> None:
    from engine.safety.tool_guard import GuardResult

    class BlockingGuard:
        def check(self, _call):
            return GuardResult(allowed=False, reason="blocked by test policy")

    async def run():
        llm = FakeLLM([
            ChatResponse(tool_calls=[_tool_call("ok", "blocked-1")]),
            ChatResponse(text="I cannot complete that operation."),
        ])
        events = [
            event
            async for event in _react_event_loop(
                llm,
                [{"role": "user", "content": "try a blocked tool"}],
                _registry(),
                tool_guard=BlockingGuard(),  # type: ignore[arg-type]
            )
        ]
        return events, llm

    events, llm = asyncio.run(run())

    assert any(event.type == EventType.TOOL_CALL_RESULT and event.data["blocked"] for event in events)
    assert any(
        message.get("role") == "system"
        and "Do not attempt to bypass" in str(message.get("content", ""))
        for message in llm.chat_calls[1]["messages"]
    )


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    raise SystemExit(1 if failures else 0)
