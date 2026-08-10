"""Text-collecting adapters over the canonical ReAct event loop.

Production code consumes :func:`react_event_loop` events directly — see
``engine/execution/orchestration/agent_loop.py``.  Nothing outside these
tests ever wanted the run's final text as a plain string.  Both adapters
used to live in ``engine/execution/react/react_loop.py`` and looked
load-bearing because nine tests called them; the call count measured how
convenient they were for assertions, not whether the engine needed them.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from engine.execution.events import EventType
from engine.execution.react.react_loop import (
    FailedAgentRunError,
    IncompleteAgentRunError,
    react_event_loop,
)


async def react_stream_loop(*args: Any, **kwargs: Any) -> AsyncGenerator[str, None]:
    """Run the canonical loop and expose committed text deltas only.

    Live streaming is handled by provisional events inside the canonical
    loop; this yields only the final committed ``TEXT_DELTA``.
    """
    async for event in react_event_loop(*args, **kwargs):
        if event.type == EventType.TEXT_DELTA:
            text = event.data.get("text", "")
            if text:
                yield str(text)
        elif event.type == EventType.INCOMPLETE:
            raise IncompleteAgentRunError(
                str(event.data.get("reason", "agent_incomplete"))
            )
        elif event.type == EventType.FAILED:
            raise FailedAgentRunError(str(event.data.get("reason", "agent_failed")))


async def react_loop(*args: Any, **kwargs: Any) -> str:
    """Run the canonical loop and collect the final assistant text."""
    return "".join([chunk async for chunk in react_stream_loop(*args, **kwargs)])
