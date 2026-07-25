from __future__ import annotations

import asyncio

import pytest

from engine.execution.react.react_loop import react_event_loop
from engine.llm.client import ChatResponse, ToolCallData
from engine.llm.events import ProviderEvent, ProviderEventType
from engine.observability.events import EventType, ExecutionEvent
from engine.replay import (
    RecordedTurn,
    RecordingLLM,
    ReplayExhaustedError,
    ReplayLLM,
    ReplayShapeError,
    load_recording,
    signature_diff,
    signature_of,
)
from engine.tool.registry import ToolRegistry


class _FakeProvider:
    """A non-streaming provider — no ``chat_events`` at all."""

    stream = False
    model = "fake"

    def __init__(self, responses: list[ChatResponse]) -> None:
        self.responses = list(responses)

    async def chat(self, messages, tools=None, prefix_cache_key=None):
        return self.responses.pop(0)

    async def close(self) -> None:
        pass


class _StreamingProvider:
    """A streaming provider — one event list per turn."""

    stream = True
    model = "fake-stream"

    def __init__(self, turns: list[list[ProviderEvent]]) -> None:
        self.turns = list(turns)

    async def chat_events(self, messages, tools=None):
        for event in self.turns.pop(0):
            yield event

    async def chat(self, messages, tools=None, prefix_cache_key=None):
        raise AssertionError("streaming provider should be driven through chat_events")

    async def close(self) -> None:
        pass


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def ok():
        return "OK"

    registry.register("ok", "Succeeds", {}, ok)
    return registry


def _turns() -> list[ChatResponse]:
    return [
        ChatResponse(tool_calls=[ToolCallData(id="c1", name="ok", arguments={})]),
        ChatResponse(text="done"),
    ]


def _stream_turn() -> list[ProviderEvent]:
    return [
        ProviderEvent(ProviderEventType.RESPONSE_CREATED, {}),
        ProviderEvent(ProviderEventType.REASONING_DELTA, {"delta": "先想一下"}),
        ProviderEvent(ProviderEventType.OUTPUT_TEXT_DELTA, {"delta": "hello"}),
        ProviderEvent(ProviderEventType.RESPONSE_COMPLETED, {"finish_reason": "stop"}),
    ]


async def _collect(llm) -> list[ExecutionEvent]:
    events = []
    async for event in react_event_loop(
        llm,
        [{"role": "user", "content": "use the ok tool"}],
        _registry(),
        max_iters=3,
    ):
        events.append(event)
    return events


async def _drain_stream(llm) -> list[ProviderEvent]:
    return [event async for event in llm.chat_events([{"role": "user", "content": "x"}])]


# ── streaming: the shape real runs actually use ────────────────────────────


def test_streaming_turns_are_recorded_and_replayed_verbatim(tmp_path):
    """真实运行是流式的，录制必须保真到事件级，否则回放的是另一条路径。"""
    path = tmp_path / "stream.jsonl"
    original = _stream_turn()

    recorder = RecordingLLM(_StreamingProvider([list(original)]), path)
    assert recorder.stream is True, "必须透传内层的流式能力，不能强制降级"

    relayed = asyncio.run(_drain_stream(recorder))
    assert [event.type for event in relayed] == [event.type for event in original]

    turns = load_recording(path)
    assert len(turns) == 1 and turns[0].is_streaming

    replay = ReplayLLM(turns)
    assert replay.stream is True
    replayed = asyncio.run(_drain_stream(replay))
    assert [(e.type, e.data) for e in replayed] == [(e.type, e.data) for e in original]


def test_recorder_does_not_invent_streaming_for_a_plain_client(tmp_path):
    """内层不支持流式时不能暴露 chat_events —— 暴露一个会失败的方法比不暴露更糟。"""
    recorder = RecordingLLM(_FakeProvider(_turns()), tmp_path / "plain.jsonl")

    assert getattr(recorder, "chat_events", None) is None


def test_half_streamed_turn_is_not_recorded(tmp_path):
    """消费者提前 break 时不该留下半个回合 —— 回放会把它当完整回合供给。"""
    path = tmp_path / "partial.jsonl"
    recorder = RecordingLLM(_StreamingProvider([_stream_turn()]), path)

    async def take_one():
        async for _ in recorder.chat_events([{"role": "user", "content": "x"}]):
            break

    asyncio.run(take_one())

    assert not path.exists() or path.read_text(encoding="utf-8").strip() == ""


def test_replaying_a_stream_through_chat_is_refused():
    llm = ReplayLLM([RecordedTurn(events=tuple(_stream_turn()))])

    with pytest.raises(ReplayShapeError):
        asyncio.run(llm.chat([{"role": "user", "content": "x"}]))


# ── non-streaming ──────────────────────────────────────────────────────────


def test_recording_then_replay_reproduces_the_same_run(tmp_path):
    path = tmp_path / "run.jsonl"

    recorded = asyncio.run(_collect(RecordingLLM(_FakeProvider(_turns()), path)))
    replayed = asyncio.run(_collect(ReplayLLM(load_recording(path))))

    assert signature_of(recorded).tools == ("ok",)
    assert signature_diff(signature_of(recorded), signature_of(replayed)) == ""


def test_recording_survives_a_tool_call_round_trip(tmp_path):
    """tool_calls must rebuild as ToolCallData, not stay raw dicts."""
    path = tmp_path / "run.jsonl"
    asyncio.run(_collect(RecordingLLM(_FakeProvider(_turns()), path)))

    turns = load_recording(path)

    assert [turn.is_streaming for turn in turns] == [False, False]
    first = turns[0].response
    assert first is not None
    assert isinstance(first.tool_calls[0], ToolCallData)
    assert first.tool_calls[0].name == "ok"
    assert turns[1].response is not None and turns[1].response.text == "done"


def test_replay_refuses_to_invent_turns_the_recording_lacks():
    llm = ReplayLLM([ChatResponse(text="only one")])

    asyncio.run(llm.chat([{"role": "user", "content": "x"}]))

    with pytest.raises(ReplayExhaustedError):
        asyncio.run(llm.chat([{"role": "user", "content": "y"}]))


def test_replay_catches_a_real_harness_behaviour_change(tmp_path):
    """Same recording, tighter iteration budget — the signature must diverge.

    This is the property the whole harness exists for: hold the model's
    responses fixed, change the loop, and see the decisions move.
    """
    path = tmp_path / "run.jsonl"
    baseline = asyncio.run(_collect(RecordingLLM(_FakeProvider(_turns()), path)))
    turns = load_recording(path)

    async def run_with_budget(max_iters: int):
        events = []
        async for event in react_event_loop(
            ReplayLLM(turns),
            [{"role": "user", "content": "use the ok tool"}],
            _registry(),
            max_iters=max_iters,
        ):
            events.append(event)
        return events

    tightened = asyncio.run(run_with_budget(1))

    diff = signature_diff(signature_of(baseline), signature_of(tightened))
    assert diff, "shrinking the iteration budget must show up as a signature diff"


# ── signature diffing ──────────────────────────────────────────────────────


def test_signature_diff_names_the_first_diverging_tool():
    expected = signature_of([
        ExecutionEvent(EventType.TOOL_CALL_START, {"name": "read_file"}),
        ExecutionEvent(EventType.DONE, {}),
    ])
    actual = signature_of([
        ExecutionEvent(EventType.TOOL_CALL_START, {"name": "shell"}),
        ExecutionEvent(EventType.DONE, {}),
    ])

    diff = signature_diff(expected, actual)

    assert "tool[0]" in diff
    assert "read_file" in diff and "shell" in diff


def test_signature_diff_reports_a_missing_event():
    expected = signature_of([
        ExecutionEvent(EventType.TOOL_CALL_START, {"name": "ok"}),
        ExecutionEvent(EventType.DONE, {}),
    ])
    actual = signature_of([
        ExecutionEvent(EventType.TOOL_CALL_START, {"name": "ok"}),
    ])

    diff = signature_diff(expected, actual)

    assert "event count" in diff
    assert signature_diff(expected, expected) == ""
