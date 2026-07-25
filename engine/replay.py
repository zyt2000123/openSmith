"""Record and replay model turns so harness logic can be regression-tested.

Replaying recorded responses removes the model's nondeterminism, which turns
"did my harness change its decisions?" into a plain equality assertion.

A recording keeps whichever shape the run actually used:

* streaming — the ``ProviderEvent`` sequence, replayed through ``chat_events``
* non-streaming — the finished ``ChatResponse``, replayed through ``chat``

Recording streaming verbatim matters because a large part of the harness only
exists on that path: the response accumulator, provisional-text commit/retract,
and mid-stream context-limit recovery. An earlier version forced non-streaming
for convenience and measurably changed behavior (the same five e2e cases went
5/5 streaming vs 2/5 forced-non-streaming), which defeats the point — a
recording has to reproduce the run, not a nearby one.

Boundary worth knowing before trusting a green replay: a recording stays valid
only while the prompt is unchanged. Recorded responses were produced by the
*old* prompt, so replaying them against a new one proves nothing about the new
prompt's quality — it only checks harness logic (truncation cut points,
compaction timing, gate verdicts, routing, tool dispatch).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Sequence

from engine.llm.contracts import ChatResponse, ToolCallData
from engine.llm.events import ProviderEvent, ProviderEventType
from engine.observability.events import EventType, ExecutionEvent


class ReplayExhaustedError(RuntimeError):
    """The harness asked for more model turns than the recording holds."""


class ReplayShapeError(RuntimeError):
    """The harness asked for a turn in the shape the recording does not hold."""


# ── serialization ──────────────────────────────────────────────────────────


def dump_response(response: ChatResponse) -> dict[str, Any]:
    return {
        "text": response.text,
        "reasoning": response.reasoning,
        "tool_calls": [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in response.tool_calls
        ],
        "usage": response.usage,
        "finish_reason": response.finish_reason,
        "raw_finish_reason": response.raw_finish_reason,
        "model": response.model,
    }


def load_response(payload: dict[str, Any]) -> ChatResponse:
    return ChatResponse(
        text=payload.get("text", ""),
        reasoning=payload.get("reasoning", ""),
        tool_calls=[
            ToolCallData(
                id=call["id"],
                name=call["name"],
                arguments=call.get("arguments") or {},
            )
            for call in payload.get("tool_calls") or ()
        ],
        usage=payload.get("usage"),
        finish_reason=payload.get("finish_reason"),
        raw_finish_reason=payload.get("raw_finish_reason"),
        model=payload.get("model", ""),
    )


def dump_event(event: ProviderEvent) -> dict[str, Any]:
    return {"type": event.type.value, "data": event.data}


def load_event(payload: dict[str, Any]) -> ProviderEvent:
    return ProviderEvent(
        ProviderEventType(payload["type"]),
        payload.get("data") or {},
    )


@dataclass(frozen=True)
class RecordedTurn:
    """One model turn, in whichever shape the recorded run used."""

    events: tuple[ProviderEvent, ...] | None = None
    response: ChatResponse | None = None

    @property
    def is_streaming(self) -> bool:
        return self.events is not None


def load_recording(path: Path | str) -> list[RecordedTurn]:
    """Read a JSONL recording into ordered turns."""
    turns: list[RecordedTurn] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if "events" in payload:
            turns.append(
                RecordedTurn(
                    events=tuple(load_event(item) for item in payload["events"])
                )
            )
        else:
            turns.append(RecordedTurn(response=load_response(payload["response"])))
    return turns


# ── recording ──────────────────────────────────────────────────────────────


class RecordingLLM:
    """Wrap a real LLMPort and append every model turn to a JSONL file.

    ``chat_events`` is exposed only when the wrapped client has it, because
    ``react_loop`` probes streaming with ``getattr(llm, "chat_events", None)``.
    Exposing a method that would fail is worse than not exposing one; and
    delegating it through ``__getattr__`` is worse still — that silently hands
    the loop the inner client's streaming method and bypasses recording
    entirely, which is exactly the bug this shape prevents.
    """

    def __init__(self, inner: Any, path: Path | str) -> None:
        self._inner = inner
        self._path = Path(path)
        if hasattr(inner, "chat_events"):
            self.chat_events = self._recording_chat_events

    def __getattr__(self, name: str) -> Any:
        # stream / capabilities / context_window / model — whatever the loop reads.
        return getattr(self._inner, name)

    def _append(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        prefix_cache_key: str | None = None,
    ) -> ChatResponse:
        response = await self._inner.chat(
            messages, tools=tools, prefix_cache_key=prefix_cache_key
        )
        self._append({"response": dump_response(response)})
        return response

    async def _recording_chat_events(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Relay the event stream while collecting the whole turn.

        The write happens once, after the stream ends. Appending per event would
        leave a half turn on the file when a consumer breaks early, and replay
        would then serve that half turn as if it were complete.
        """
        collected: list[dict[str, Any]] = []
        async for event in self._inner.chat_events(messages, tools=tools):
            collected.append(dump_event(event))
            yield event
        self._append({"events": collected})

    async def close(self) -> None:
        await self._inner.close()


# ── replay ─────────────────────────────────────────────────────────────────


class ReplayLLM:
    """Serve recorded turns in order so a run becomes deterministic.

    ``stream`` and the presence of ``chat_events`` follow the recording, so the
    loop takes the same path it took when the turns were captured.
    """

    model = "replay"

    def __init__(self, turns: Sequence[RecordedTurn | ChatResponse]) -> None:
        self._turns = [
            turn if isinstance(turn, RecordedTurn) else RecordedTurn(response=turn)
            for turn in turns
        ]
        self._index = 0
        self.stream = bool(self._turns) and self._turns[0].is_streaming
        if self.stream:
            self.chat_events = self._replay_chat_events

    @property
    def turns_consumed(self) -> int:
        return self._index

    def _next_turn(self) -> RecordedTurn:
        if self._index >= len(self._turns):
            raise ReplayExhaustedError(
                f"harness requested model turn {self._index + 1} but the recording "
                f"holds {len(self._turns)} — the loop now takes more turns than "
                "when this case was recorded"
            )
        turn = self._turns[self._index]
        self._index += 1
        return turn

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        prefix_cache_key: str | None = None,
    ) -> ChatResponse:
        turn = self._next_turn()
        if turn.response is None:
            raise ReplayShapeError(
                f"turn {self._index} was recorded as a stream — replay it through "
                "chat_events, not chat"
            )
        return turn.response

    async def _replay_chat_events(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        turn = self._next_turn()
        if turn.events is None:
            raise ReplayShapeError(
                f"turn {self._index} was recorded non-streaming — replay it "
                "through chat, not chat_events"
            )
        for event in turn.events:
            yield event

    async def close(self) -> None:
        pass


# ── assertions ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RunSignature:
    """The part of a run worth asserting on: what fired, and which tools ran."""

    events: tuple[str, ...]
    tools: tuple[str, ...]


def signature_of(events: Iterable[ExecutionEvent]) -> RunSignature:
    collected = list(events)
    return RunSignature(
        events=tuple(event.type.value for event in collected),
        tools=tuple(
            str(event.data.get("name", ""))
            for event in collected
            if event.type is EventType.TOOL_CALL_START
        ),
    )


def signature_diff(expected: RunSignature, actual: RunSignature) -> str:
    """Return a readable first divergence, or "" when the runs match."""
    if expected == actual:
        return ""
    parts: list[str] = []
    if expected.tools != actual.tools:
        parts.append(_first_divergence("tool", expected.tools, actual.tools))
    if expected.events != actual.events:
        parts.append(_first_divergence("event", expected.events, actual.events))
    return "\n".join(parts)


def _first_divergence(
    label: str, expected: Sequence[str], actual: Sequence[str]
) -> str:
    for index, (left, right) in enumerate(zip(expected, actual)):
        if left != right:
            return f"{label}[{index}]: expected {left!r}, got {right!r}"
    return f"{label} count: expected {len(expected)}, got {len(actual)}"
