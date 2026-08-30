"""Record and replay LLM turns so harness logic can be regression-tested.

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
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

from engine.llm.contracts import (
    ChatResponse,
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_MAX_OUTPUT_TOKENS,
    ModelLimits,
    ProviderCapabilities,
    ToolCallData,
)
from engine.llm.events import ProviderEvent, ProviderEventType

logger = logging.getLogger(__name__)


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
    try:
        event_type = ProviderEventType(payload["type"])
    except (ValueError, TypeError, KeyError):
        # A future release can rename or remove an event type; one such line
        # must not make every old recording unloadable.  Skip it, mirroring the
        # malformed-line tolerance of load_recording.
        raise ValueError(f"unknown provider event type: {payload.get('type')!r}")
    return ProviderEvent(
        event_type,
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
    """Read a JSONL recording into ordered turns.

    A crash during append can leave a truncated final line.  Malformed or
    non-object lines are skipped (with a warning) so the rest of the recording
    stays loadable instead of failing wholesale.
    """
    recording_path = Path(path)
    turns: list[RecordedTurn] = []
    for line in recording_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            logger.warning(
                "skipping malformed recording line in %s", recording_path.name
            )
            continue
        if not isinstance(payload, dict):
            logger.warning(
                "skipping non-object recording line in %s", recording_path.name
            )
            continue
        if "events" in payload:
            events: list[ProviderEvent] = []
            for item in payload["events"]:
                try:
                    events.append(load_event(item))
                except ValueError:
                    logger.warning(
                        "skipping unknown event in %s", recording_path.name
                    )
                    continue
            if events:
                turns.append(RecordedTurn(events=tuple(events)))
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
        self._lock = threading.Lock()
        if hasattr(inner, "chat_events"):
            self.chat_events = self._recording_chat_events

    def __getattr__(self, name: str) -> Any:
        # stream / capabilities / context_window / model — whatever the loop reads.
        return getattr(self._inner, name)

    def _append(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        # One O_APPEND write per turn.  An earlier version rewrote the whole
        # file through a temp file and a rename to buy crash tolerance, which
        # made the recording cost O(N²) IO over a session — and both callers
        # invoke this synchronously from an async method, so the stall lands on
        # the event loop thread that is streaming the very turn being recorded.
        # Appending gives the same tolerance for free: a crash can only
        # truncate the final line, which load_recording already skips.
        # The lock still serializes overlapping turns on one client (e.g. a
        # compaction summary while the main loop records) so two payloads
        # cannot interleave inside one line.
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(line)

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
        prefix_cache_key: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Relay the event stream while collecting the whole turn.

        The write happens once, after the stream ends. Appending per event would
        leave a half turn on the file when a consumer breaks early, and replay
        would then serve that half turn as if it were complete.

        ``prefix_cache_key`` is forwarded only when present so the wrapper also
        works over minimal inner stubs that predate that parameter.
        """
        collected: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"tools": tools}
        if prefix_cache_key is not None:
            kwargs["prefix_cache_key"] = prefix_cache_key
        async for event in self._inner.chat_events(messages, **kwargs):
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
    provider = "replay"

    def __init__(self, turns: Sequence[RecordedTurn | ChatResponse]) -> None:
        self._turns = [
            turn if isinstance(turn, RecordedTurn) else RecordedTurn(response=turn)
            for turn in turns
        ]
        self._index = 0
        self.stream = bool(self._turns) and self._turns[0].is_streaming
        if self.stream:
            self.chat_events = self._replay_chat_events
        # A recording does not capture the original route's capacity facts, so
        # replay reports the same conservative defaults the engine falls back
        # to for any client that omits them.  Declaring them explicitly keeps
        # ReplayLLM a complete LLMPort instead of relying on getattr fallbacks.
        self.capabilities = ProviderCapabilities(
            streaming=self.stream,
            tool_calls=True,
            prefix_cache_key=False,
        )
        self.context_window = DEFAULT_CONTEXT_WINDOW
        self.context_window_declared = False
        self.max_output_tokens = DEFAULT_MAX_OUTPUT_TOKENS
        self.max_output_tokens_declared = False
        self.limits = ModelLimits(
            context_window=self.context_window,
            context_window_declared=self.context_window_declared,
            max_output_tokens=self.max_output_tokens,
            max_output_tokens_declared=self.max_output_tokens_declared,
        )

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
            # ``_next_turn`` has already advanced ``_index``, so the turn that
            # just failed the shape check is ``_index - 1``, not ``_index``.
            raise ReplayShapeError(
                f"turn {self._index - 1} was recorded as a stream — replay it through "
                "chat_events, not chat"
            )
        return turn.response

    async def _replay_chat_events(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        prefix_cache_key: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        # ``prefix_cache_key`` is accepted for LLMPort signature parity but
        # deliberately ignored: recorded turns are served in order, they are
        # never regenerated, so no cache hint is meaningful here.
        turn = self._next_turn()
        if turn.events is None:
            # ``_next_turn`` has already advanced ``_index``, so the turn that
            # just failed the shape check is ``_index - 1``, not ``_index``.
            raise ReplayShapeError(
                f"turn {self._index - 1} was recorded non-streaming — replay it "
                "through chat, not chat_events"
            )
        for event in turn.events:
            yield event

    async def close(self) -> None:
        pass
