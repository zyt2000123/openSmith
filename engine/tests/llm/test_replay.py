from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from engine.execution.react.react_loop import react_event_loop
from engine.execution.run_signature import signature_diff, signature_of
from engine.llm.client import ChatResponse, ToolCallData
from engine.llm.events import ProviderEvent, ProviderEventType
from engine.execution.events import ExecutionEvent
from engine.llm.replay import (
    RecordedTurn,
    RecordingLLM,
    ReplayExhaustedError,
    ReplayLLM,
    ReplayShapeError,
    load_recording,
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


def test_recorder_forwards_prefix_cache_key_to_inner_stream(tmp_path):
    """react_loop 在 prefix cache 开启时会给 chat_events 传 key,录制器必须透传。"""
    path = tmp_path / "with-cache.jsonl"
    seen: dict[str, object] = {}

    class _CacheStreamingProvider:
        stream = True
        model = "fake-cache"

        async def chat_events(self, messages, tools=None, prefix_cache_key=None):
            seen["prefix_cache_key"] = prefix_cache_key
            yield ProviderEvent(ProviderEventType.RESPONSE_CREATED, {})
            yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED, {})

        async def close(self) -> None:
            pass

    recorder = RecordingLLM(_CacheStreamingProvider(), path)

    async def drain():
        async for _ in recorder.chat_events(
            [{"role": "user", "content": "x"}],
            prefix_cache_key="stable-prefix",
        ):
            pass

    asyncio.run(drain())

    assert seen["prefix_cache_key"] == "stable-prefix"
    assert path.read_text(encoding="utf-8").strip(), "the turn must still be recorded"


def test_replaying_a_stream_through_chat_is_refused():
    llm = ReplayLLM([RecordedTurn(events=tuple(_stream_turn()))])

    with pytest.raises(ReplayShapeError):
        asyncio.run(llm.chat([{"role": "user", "content": "x"}]))


def test_replay_shape_error_names_the_offending_turn_when_chat() -> None:
    """诊断必须指向刚被消费的回合(off-by-one 修复),而不是下一个。"""
    llm = ReplayLLM([RecordedTurn(events=tuple(_stream_turn()))])

    with pytest.raises(ReplayShapeError) as exc_info:
        asyncio.run(llm.chat([{"role": "user", "content": "x"}]))

    assert "turn 0" in str(exc_info.value)
    assert "turn 1" not in str(exc_info.value)


def test_replay_shape_error_names_the_offending_turn_when_streaming() -> None:
    """混合录音里,形状不匹配的回合编号必须准确。"""
    llm = ReplayLLM([
        RecordedTurn(events=tuple(_stream_turn())),
        ChatResponse(text="done"),
    ])

    # First turn is streaming: consume it through chat_events — succeeds.
    asyncio.run(_drain_stream(llm))
    # Second turn is non-streaming; asking for a stream must name turn 1.
    with pytest.raises(ReplayShapeError) as exc_info:
        asyncio.run(_drain_stream(llm))

    assert "turn 1" in str(exc_info.value)
    assert "turn 2" not in str(exc_info.value)


def test_load_recording_skips_a_truncated_final_line(tmp_path) -> None:
    """崩溃留下的残缺末行不应让整份录音无法加载。"""
    path = tmp_path / "corrupt.jsonl"
    path.write_text(
        '{"response":{"text":"ok"}}\n'
        '{"response":{"te\n'        # truncated mid-append line
        '{"response":{"text":"also ok"}}\n',
        encoding="utf-8",
    )

    turns = load_recording(path)

    assert [turn.response.text for turn in turns] == ["ok", "also ok"]


def test_load_recording_skips_unknown_event_types(tmp_path) -> None:
    """A future release can rename an event type; one such line must not make
    every old recording unloadable."""
    path = tmp_path / "future.jsonl"
    path.write_text(
        '{"events":[{"type":"response.output_text.delta","data":{"delta":"known"}},'
        '{"type":"completely.new.event.type","data":{"x":1}}]}\n'
        '{"response":{"text":"plain turn"}}\n',
        encoding="utf-8",
    )

    turns = load_recording(path)

    assert turns[0].is_streaming
    assert [event.type.value for event in turns[0].events] == ["response.output_text.delta"]
    assert turns[1].response is not None
    assert turns[1].response.text == "plain turn"


def test_recorder_appends_atomically_without_leaving_temp_file(tmp_path) -> None:
    """原子追加:录制文件恒可加载,且不残留临时文件。"""
    path = tmp_path / "atomic.jsonl"
    recorder = RecordingLLM(_FakeProvider(_turns()), path)
    asyncio.run(_collect(recorder))

    turns = load_recording(path)
    assert [turn.response.text for turn in turns] == ["", "done"]
    assert not list(tmp_path.glob("*.tmp"))


def test_recorder_appends_without_reading_the_recording_back(tmp_path, monkeypatch) -> None:
    """每轮追加必须与已录长度无关。

    读改写的成本随文件线性增长(整场录制 O(N²)),而且它是从 async 方法里同步
    调用的 —— 停顿落在正在推这一轮流的事件循环线程上。
    """
    path = tmp_path / "grow.jsonl"
    path.write_text('{"response":{"text":"earlier session"}}\n', encoding="utf-8")
    recorder = RecordingLLM(_FakeProvider(_turns()), path)

    real_read_text = Path.read_text

    def _forbid_reading_the_recording(self: Path, *args, **kwargs):
        assert self != path, "追加一轮不该把整份录制读回来"
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _forbid_reading_the_recording)
    asyncio.run(recorder.chat([{"role": "user", "content": "x"}]))
    asyncio.run(recorder.chat([{"role": "user", "content": "y"}]))
    monkeypatch.undo()

    # 追加不是覆盖:上一场录制的内容必须原样留着。
    assert [turn.response.text for turn in load_recording(path)] == [
        "earlier session",
        "",
        "done",
    ]


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
