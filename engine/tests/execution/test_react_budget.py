from __future__ import annotations

import asyncio
import json

from engine.execution.react.react_loop import react_event_loop as _react_event_loop
from engine.tests.execution.react_text_adapters import (
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
    CONVERSATION_KEEP_RECENT,
    MAX_COMPACTION_FAILURES,
    MAX_FAILED_TOOL_RECOVERY_ITERS,
    MAX_IDENTICAL_TOOL_ERRORS,
    MAX_PREFLIGHT_CHALLENGE_ITERS,
    trim_conversation_to_message_cap,
)
from engine.safety.fact_gate import FactGate, FactGateContext
from engine.skill.executor import execute_skill_events
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


def test_react_event_loop_loads_only_requested_tool_schemas() -> None:
    async def read_file(path: str) -> str:
        return f"read {path}"

    async def delete_file(path: str) -> str:
        return f"deleted {path}"

    async def run():
        registry = ToolRegistry(lazy_tool_schemas=True)
        registry.register(
            "read_file", "Read one file",
            {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            read_file,
        )
        registry.register(
            "delete_file", "Delete one file",
            {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            delete_file,
        )
        llm = FakeLLM([
            ChatResponse(tool_calls=[ToolCallData(
                id="load-1", name="get_tool_schema", arguments={"name": "read_file"},
            )]),
            ChatResponse(tool_calls=[ToolCallData(
                id="read-1", name="read_file", arguments={"path": "notes.txt"},
            )]),
            ChatResponse(text="done"),
        ])
        events = [event async for event in _react_event_loop(
            llm, [{"role": "user", "content": "Read notes"}], registry,
        )]
        return llm, events

    llm, events = asyncio.run(run())

    first_tools = llm.chat_calls[0]["tools"]
    assert [tool["function"]["name"] for tool in first_tools] == ["get_tool_schema"]
    second_tools = llm.chat_calls[1]["tools"]
    assert [tool["function"]["name"] for tool in second_tools] == [
        "get_tool_schema", "read_file",
    ]
    assert "delete_file" not in json.dumps(second_tools)
    schema_result = next(
        message for message in llm.chat_calls[1]["messages"]
        if message.get("role") == "tool" and message.get("tool_call_id") == "load-1"
    )
    assert '"name": "read_file"' in schema_result["content"]
    assert any(event.type is EventType.TEXT_DELTA for event in events)


def test_react_event_loop_runs_a_tool_called_without_the_schema_handshake() -> None:
    """Skipping ``get_tool_schema`` must not read as a disabled capability.

    ``Tool disabled: <name>`` is terminal wording: a run that hit it abandoned
    the tool for the rest of the conversation instead of loading its schema.
    """
    async def read_file(path: str) -> str:
        return f"read {path}"

    async def run():
        registry = ToolRegistry(lazy_tool_schemas=True)
        registry.register(
            "read_file", "Read one file",
            {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            read_file,
        )
        llm = FakeLLM([
            ChatResponse(tool_calls=[ToolCallData(
                id="read-1", name="read_file", arguments={"path": "notes.txt"},
            )]),
            ChatResponse(text="done"),
        ])
        events = [event async for event in _react_event_loop(
            llm, [{"role": "user", "content": "Read notes"}], registry,
        )]
        return llm, events

    llm, events = asyncio.run(run())

    result = next(
        message for message in llm.chat_calls[1]["messages"]
        if message.get("role") == "tool" and message.get("tool_call_id") == "read-1"
    )
    assert result["content"] == "read notes.txt"
    # The schema joins the exposed list, so the next turn can call it directly.
    assert [tool["function"]["name"] for tool in llm.chat_calls[1]["tools"]] == [
        "get_tool_schema", "read_file",
    ]
    assert any(event.type is EventType.TEXT_DELTA for event in events)


def test_react_event_loop_still_refuses_a_tool_outside_the_allowlist() -> None:
    """Completing the handshake must not widen the profile/identity allowlist."""
    async def read_file(path: str) -> str:
        return f"read {path}"

    async def delete_file(path: str) -> str:
        return f"deleted {path}"

    async def run():
        registry = ToolRegistry(lazy_tool_schemas=True)
        registry.register(
            "read_file", "Read one file",
            {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            read_file,
        )
        registry.register(
            "delete_file", "Delete one file",
            {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            delete_file,
        )
        registry.set_enabled(["read_file"])
        llm = FakeLLM([
            ChatResponse(tool_calls=[ToolCallData(
                id="del-1", name="delete_file", arguments={"path": "notes.txt"},
            )]),
            ChatResponse(text="done"),
        ])
        events = [event async for event in _react_event_loop(
            llm, [{"role": "user", "content": "Delete notes"}], registry,
        )]
        return llm, events

    llm, events = asyncio.run(run())

    result = next(
        message for message in llm.chat_calls[1]["messages"]
        if message.get("role") == "tool" and message.get("tool_call_id") == "del-1"
    )
    assert result["content"] == "Tool disabled: delete_file"
    assert "delete_file" not in json.dumps(llm.chat_calls[1]["tools"])
    assert any(event.type is EventType.TEXT_DELTA for event in events)


def test_time_tool_result_is_appended_as_user_context() -> None:
    async def current_time():
        return '{"local_time":"2026-08-07T12:00:00+08:00","timezone":"Asia/Shanghai"}'

    async def run():
        registry = ToolRegistry()
        registry.register("get_current_time", "Gets the current time", {}, current_time)
        llm = FakeLLM([
            ChatResponse(tool_calls=[_tool_call("get_current_time")]),
            ChatResponse(text="It is noon."),
        ])
        events = [event async for event in _react_event_loop(
            llm,
            [{"role": "user", "content": "What time is it?"}],
            registry,
        )]
        return llm, events

    llm, events = asyncio.run(run())

    follow_up_messages = llm.chat_calls[1]["messages"]
    assert {"role": "tool", "tool_call_id": "call-1", "content": "Time data was added to the user context."} in follow_up_messages
    assert {
        "role": "user",
        "content": "[Current time]\n{\"local_time\":\"2026-08-07T12:00:00+08:00\",\"timezone\":\"Asia/Shanghai\"}",
    } in follow_up_messages
    assert any(event.type is EventType.TEXT_DELTA for event in events)


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


def test_react_event_loop_rerenders_a_reply_that_mixes_streamed_and_fallback_parts():
    """A reply assembled from a non-streamed fragment plus a streamed one must
    reach the screen whole.

    ``already_streamed`` used to be one flag for the join, so the streamed
    continuation marked its unstreamed sibling as rendered too; the consumer
    skips rendering those, and the fallback half was persisted but never shown.
    """

    class StreamThenFallbackLLM(StreamingFakeLLM):
        """A ``None`` turn fails the stream before any semantic delta — exactly
        the condition under which react_loop retries with non-streaming chat."""

        def __init__(self, event_turns, responses) -> None:
            super().__init__(event_turns)
            self.responses = list(responses)

        async def chat_events(self, messages, tools=None, prefix_cache_key=None):
            turn = self.event_turns.pop(0)
            if turn is None:
                raise LLMResponseError("stream died")
            for event in turn:
                yield event

    async def run():
        llm = StreamThenFallbackLLM(
            [
                None,
                [
                    ProviderEvent(ProviderEventType.OUTPUT_TEXT_DELTA, {"delta": "第二段。"}),
                    ProviderEvent(
                        ProviderEventType.RESPONSE_COMPLETED,
                        {"finish_reason": "stop", "raw_finish_reason": "stop"},
                    ),
                ],
            ],
            [ChatResponse(text="第一段：", finish_reason="length")],
        )
        return [
            event
            async for event in _react_event_loop(
                llm,
                [{"role": "user", "content": "完整回答"}],
                _registry(),
                max_iters=2,
            )
        ]

    events = asyncio.run(run())
    drafts = [event.data for event in events if event.type == EventType.PROVISIONAL_TEXT_DELTA]
    retractions = [event.data for event in events if event.type == EventType.PROVISIONAL_RETRACT]
    commits = [event.data for event in events if event.type == EventType.PROVISIONAL_COMMIT]
    finals = [event.data for event in events if event.type == EventType.TEXT_DELTA]

    assert [draft["text"] for draft in drafts] == ["第二段。"]
    # No already_streamed marker: the consumer must render this, or 第一段 never
    # reaches the screen.
    assert finals == [{"text": "第一段：第二段。"}]
    # The streamed half is on screen as a draft, so it is withdrawn before the
    # join is re-sent — committing it and re-sending would render it twice.
    assert retractions == [
        {
            "provision_id": drafts[0]["provision_id"],
            "reason": "unstreamed_fragment_pending",
        },
    ]
    assert commits == []


def test_react_event_loop_rejects_active_work_that_cannot_fit_without_rewriting_it():
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

    assert llm.compactor_calls == 0
    assert context[-1]["fit_status"] == "unfit_request"
    assert any(
        event.type is EventType.INCOMPLETE
        and event.data["reason"] == "context_capacity_exhausted"
        for event in events
    )


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


def test_context_limit_recovery_retracts_abandoned_provisional_draft():
    """A stream that emits deltas and then hits a context-limit error must
    retract the abandoned draft before the recovered stream runs; otherwise
    both ids get committed at finish and the client keeps rendering text that
    was discarded."""

    class DraftThenContextLimitLLM(FakeLLM):
        stream = True

        def __init__(self) -> None:
            super().__init__([])
            self.stream_attempts = 0

        async def chat_events(self, messages, tools=None):
            self.stream_attempts += 1
            if self.stream_attempts == 1:
                yield ProviderEvent(
                    ProviderEventType.OUTPUT_TEXT_DELTA,
                    {"delta": "abandoned draft"},
                )
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

    async def run():
        llm = DraftThenContextLimitLLM()
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
    provision_ids = [
        event.data["provision_id"]
        for event in events
        if event.type == EventType.PROVISIONAL_TEXT_DELTA
    ]
    retracted_ids = [
        event.data["provision_id"]
        for event in events
        if event.type == EventType.PROVISIONAL_RETRACT
    ]
    committed_ids = [
        event.data["provision_id"]
        for event in events
        if event.type == EventType.PROVISIONAL_COMMIT
    ]

    assert len(provision_ids) == 2
    # The abandoned draft is retracted; only the recovered stream's draft is
    # committed — never both.
    assert retracted_ids == provision_ids[:1]
    assert committed_ids == provision_ids[1:]
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
    assert context["actions"] == []


def test_context_usage_flags_a_turn_the_provider_never_priced():
    """CONTEXT_USAGE.estimated exists to separate a provider count from a local
    guess.  The usage fallback hands ``receipt.estimated_input_tokens`` back
    through the very argument that decides the flag, so on every relay that
    drops ``stream_options.include_usage`` the guess used to report itself as
    a measured number.
    """

    async def run():
        llm = FakeLLM([ChatResponse(text="完成")])
        return [
            event
            async for event in _react_event_loop(
                llm,
                [{"role": "user", "content": "hello"}],
                _registry(),
                max_iters=1,
            )
        ]

    events = asyncio.run(run())
    usage = [event.data for event in events if event.type == EventType.TOKEN_USAGE][-1]
    context = [event.data for event in events if event.type == EventType.CONTEXT_USAGE][-1]

    assert usage["usage_reported"] == 0
    assert context["context_tokens"] == usage["input_tokens"] > 0
    assert context["estimated"] is True


def test_context_usage_reports_which_fitting_actions_ran():
    """A shrunk request must say WHAT was dropped, not merely that it shrank.

    fit_status="compacted" covers both a cheap tool-output prune and a full
    LLM re-summary of the history, and "recovered" means messages were
    deleted outright.  Only fit.actions tells them apart, and it used to
    reach the trace on the UNFIT path alone — so the one question a trace
    gets asked after "the agent forgot my opening question" was the one it
    could not answer.
    """

    async def run():
        llm = FakeLLM([ChatResponse(text="done")], stream_chunks=["done"])
        conversation: list[dict] = [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [ToolCallData(id="c1", name="ok", arguments={})],
            },
        ]
        # 3 x 6000 chars: walking backwards the newest two fill the 8000-char
        # protect threshold, so the oldest is pruned — 6000 chars, over the
        # 2000-char minimum that makes a prune worth doing.
        for _ in range(3):
            conversation.append(
                {"role": "tool", "tool_call_id": "c1", "content": "x" * 6000}
            )
        events = []
        async for event in _react_event_loop(
            llm,
            conversation,
            _registry(),
            max_iters=1,
        ):
            events.append(event)
        return events

    events = asyncio.run(run())
    context = [
        event for event in events if event.type == EventType.CONTEXT_USAGE
    ][-1].data
    assert any(
        action.startswith("pruned_tool_output_chars:")
        for action in context["actions"]
    ), context["actions"]


def test_react_event_loop_rejects_an_oversized_active_request_before_provider_call():
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

    assert llm.chat_calls == []
    assert any(
        event.type is EventType.INCOMPLETE
        and event.data["reason"] == "context_capacity_exhausted"
        and event.data["fit_status"] == "unfit_request"
        for event in events
    )


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
    """Driven through execute_skill_events — the entry pipeline.py actually uses.

    The subject is react_event_loop's budget accounting, not the skill wrapper;
    the wrapper is only the vehicle.  It used to run through execute_skill(),
    whose injected react_loop_fn no longer has any production implementation.
    """

    async def run():
        skill = SkillBody(meta=SkillMeta(name="sample"), content="Use tools if needed.")
        llm = FakeLLM([
            ChatResponse(tool_calls=[_tool_call()]),
            ChatResponse(text="skill recovered"),
        ])
        chunks = []
        async for event in execute_skill_events(
            skill,
            llm,
            _registry(),
            [{"role": "user", "content": "try a tool"}],
            {"user_message": "try a tool"},
            max_iters=1,
            react_event_loop_fn=_react_event_loop,
        ):
            if event.type == EventType.TEXT_DELTA:
                chunks.append(str(event.data.get("text", "")))
        return "".join(chunks)

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


def test_react_event_loop_conversation_pruning_keeps_contract_and_request():
    """Pruning keeps the system contract and the turn being executed.

    It used to keep ``conversation[:2]`` — right only when the run opens with
    [system, request].  In a continuing session index 1 is the *oldest* history
    message, so pinning it wasted the head slot on stale context; what must
    survive is the newest user turn.
    """
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
    newest_request = {
        "role": "user",
        "content": f"follow-up-{CONVERSATION_HARD_LIMIT - 1}",
    }
    assert messages[0] == {"role": "system", "content": "system prompt"}
    assert newest_request in messages
    # 条数不精确断言：切点还要对齐到 user 轮，落点随历史形状浮动。锁的是
    # "确实裁短了"且不超出尾窗预算，而不是某个结构细节。
    assert CONVERSATION_KEEP_RECENT - 2 <= len(messages) <= 1 + CONVERSATION_KEEP_RECENT


def test_trimmed_conversation_always_opens_on_a_user_turn():
    """裁剪后首条非 system 消息必须是 user 轮。

    Anthropic 直接拒绝不以 user 开头的对话，而那个错误不是 context-limit
    错误，所以它会让整个 run 失败而不是触发重试。旧的固定 conversation[:2]
    head 是碰巧满足的（[system, *历史, 请求] 的 index 1 是历史里的 user 轮）；
    改成按角色选 head 之后这个巧合没了：边界回退扫描会心安理得地停在
    assistant 上，于是历史顶到 40 条上限的会话每一轮都在第一次 provider
    调用上失败。
    """
    # 历史顶到 server 侧 _HISTORY_LIMIT 的稳态形状，不是边界值。
    for history_len in (38, 39, 40):
        for with_summary in (False, True):
            conversation = [{"role": "system", "content": "system prompt"}]
            if with_summary:
                conversation.append(
                    {"role": "user", "content": "[Session context summary]\n…"}
                )
            for i in range(history_len):
                conversation.append({
                    "role": "user" if i % 2 == 0 else "assistant",
                    "content": f"old-{i}",
                })
            conversation.append({"role": "user", "content": "current request"})

            trimmed = trim_conversation_to_message_cap(conversation)

            non_system = [m for m in trimmed if m.get("role") != "system"]
            assert non_system, f"history={history_len} summary={with_summary}"
            assert non_system[0]["role"] == "user", (
                f"history={history_len} summary={with_summary} "
                f"opens on {non_system[0]['role']}"
            )
            assert {"role": "user", "content": "current request"} in trimmed


def test_react_event_loop_keeps_the_request_behind_session_history():
    """The live request must survive a tool marathon, history in front of it.

    Production layout is [system, *session history, request] (agent_loop), and
    the cap ran every iteration: as tool traffic pushed the request left out of
    the tail window it was silently deleted, leaving the model to call tools
    against a days-old user turn for the rest of the run.
    """
    request = "current request: 把 A 改成 B"

    async def run():
        history = [
            {"role": "user" if i % 2 else "assistant", "content": f"old-{i}"}
            for i in range(30)
        ]
        conversation = [
            {"role": "system", "content": "system prompt"},
            *history,
            {"role": "user", "content": request},
        ]
        # 每轮迭代 +2 条消息，裁剪后回到 30 条：要跑到第三次裁剪
        # （旧实现正是在那一次把请求切掉的）需要约 16 轮工具调用。
        turns = [
            ChatResponse(text="", tool_calls=[_tool_call("ok", f"call-{i}")])
            for i in range(20)
        ]
        turns.append(ChatResponse(text="done"))
        llm = FakeLLM(turns)
        async for _event in _react_event_loop(
            llm,
            conversation,
            _registry(),
            max_iters=25,
        ):
            pass
        return llm.chat_calls

    calls = asyncio.run(run())
    assert len(calls) > 16, "test must run long enough to trim three times"
    for index, call in enumerate(calls):
        contents = [message.get("content") for message in call["messages"]]
        assert request in contents, f"request lost from provider call #{index}"


def test_one_failed_compaction_does_not_disable_it_for_the_whole_run():
    """A transient compaction failure must not latch the run into hard trimming.

    Compaction runs its own LLM call; one timeout or one truncated summary used
    to set model_compaction_enabled=False with no way back, so the rest of a
    60-iteration run could only delete history instead of summarizing it.
    """
    class FlakyCompactionLLM(FakeLLM):
        limits = ModelLimits(
            context_window=8_192,
            context_window_declared=True,
            max_output_tokens=1_024,
            max_output_tokens_declared=True,
        )

        def __init__(self, responses):
            super().__init__(responses)
            self.compaction_attempts = 0

        async def chat(self, messages, tools=None, prefix_cache_key=None):
            summarizing = any(
                "summarizing a conversation" in (m.get("content") or "")
                for m in messages
                if m.get("role") == "system"
            )
            if summarizing:
                self.compaction_attempts += 1
                raise LLMResponseError("compaction provider timeout")
            return await super().chat(messages, tools, prefix_cache_key)

    async def run():
        conversation = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "证" * 4_000},
            {"role": "assistant", "content": "earlier work"},
            {"role": "user", "content": "current request"},
        ]
        llm = FlakyCompactionLLM([
            ChatResponse(text="", tool_calls=[_tool_call("ok", "call-1")]),
            ChatResponse(text="done"),
        ])
        async for _event in _react_event_loop(llm, conversation, _registry(), max_iters=5):
            pass
        return llm.compaction_attempts

    attempts = asyncio.run(run())
    # 下界：一次瞬时失败不该让整个 run 退化为删除式裁剪。
    assert attempts >= 2, "compaction must be retried after one transient failure"
    # 上界同样是不变量：真压不动的对话若每轮都重试，就是每轮白烧一次压缩
    # LLM 调用 —— 闩锁存在的理由。只锁下界的话，把闩锁整个删掉也照过。
    assert attempts == MAX_COMPACTION_FAILURES, (
        f"compaction must latch off after {MAX_COMPACTION_FAILURES} failures "
        f"(attempted {attempts})"
    )


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
    # 这条测试只管配对完整（R8 回归）。"请求存活"由
    # test_react_event_loop_keeps_the_request_behind_session_history 负责 ——
    # 这里的请求是对话最后一条，天然落在尾窗内，在此断言它存活是空转。
    _assert_tool_pairing_intact(messages)
    assert messages[0]["content"] == "system prompt"
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
