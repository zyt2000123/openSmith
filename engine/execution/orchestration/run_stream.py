"""A small, typed run-stream result inspired by the Agents SDK contract."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from typing import AsyncGenerator, Awaitable, Callable

from engine.observability import EventType, ExecutionEvent


class _AgentRunEventIterator(AsyncIterator[ExecutionEvent]):
    """Expose an event generator while handling an explicit pre-start close."""

    def __init__(
        self,
        stream: "AgentRunStream",
        events: AsyncGenerator[ExecutionEvent, None],
    ) -> None:
        self._stream = stream
        self._events = events
        self._started = False
        self._closed = False

    def __aiter__(self) -> "_AgentRunEventIterator":
        return self

    async def __anext__(self) -> ExecutionEvent:
        if self._closed:
            raise StopAsyncIteration
        self._started = True
        try:
            return await anext(self._events)
        except StopAsyncIteration:
            self._closed = True
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._started:
            await self._stream._close_before_start()
        await self._events.aclose()


class AgentRunStream:
    """Own one agent run's event iterator and expose its terminal state.

    Consumers must drain :meth:`stream_events` before relying on
    ``is_complete``.  This mirrors the important Agents SDK rule that the
    final visible text token is not necessarily the end of the run: persistence
    and service cleanup may still be running.
    """

    def __init__(
        self,
        run_id: str,
        events: AsyncIterator[ExecutionEvent],
        *,
        on_unstarted_close: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.run_id = run_id
        self._events = events
        self._on_unstarted_close = on_unstarted_close
        self._unstarted_closed = False
        self._closed_before_start = False
        self._active_iterator: _AgentRunEventIterator | None = None
        self._consumed = False
        self.is_complete = False
        self.status: str | None = None
        self.reason: str | None = None

    def stream_events(self) -> _AgentRunEventIterator:
        """Yield the run events once, updating terminal state on completion."""
        if self._closed_before_start:
            raise RuntimeError("AgentRunStream was closed before it was started.")
        if self._consumed:
            raise RuntimeError("AgentRunStream events can only be consumed once.")
        self._consumed = True
        iterator = _AgentRunEventIterator(self, self._iterate_events())
        self._active_iterator = iterator
        return iterator

    async def aclose(self) -> None:
        """Stop this run when its owning transport disconnects."""
        if self._active_iterator is not None:
            await self._active_iterator.aclose()
            return
        await self._close_before_start()

    async def _close_before_start(self) -> None:
        if self._closed_before_start:
            return
        self._closed_before_start = True
        if self._unstarted_closed or self._on_unstarted_close is None:
            return
        self._unstarted_closed = True
        await self._on_unstarted_close()

    async def _iterate_events(self) -> AsyncGenerator[ExecutionEvent, None]:
        try:
            async for event in self._events:
                if event.type == EventType.RUN_FINISHED:
                    status = event.data.get("status")
                    reason = event.data.get("reason")
                    self.status = status if isinstance(status, str) else "failed"
                    self.reason = reason if isinstance(reason, str) else None
                    self.is_complete = True
                yield event
        finally:
            close = getattr(self._events, "aclose", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result
