"""Public Interface for one Engine execution.

Callers outside :mod:`engine` should import run contracts and entry points
from this module.  The orchestration, pipeline, and ReAct packages are
Implementation details.
"""

from __future__ import annotations

from .assets import validate_execution_assets
from .events import (
    EventType,
    ExecutionEvent,
    RunEventObserver,
    RunObservationContext,
    RunObservationFactory,
    raw_text_delta,
)
from .hooks import HookManager, HookType
from .orchestration.lifecycle import (
    reply_with_runtime,
    resume_stream_with_runtime,
    run_memory_daily_tick,
    run_memory_idle_tick,
    run_stream_with_runtime,
)
from .orchestration.run_state import (
    RunStateError,
    RunStateStore,
    RunStateTransitionError,
    RunStatus,
)
from .orchestration.run_stream import AgentRunStream
from .orchestration.runtime import (
    EngineRequest,
    EngineResult,
    RuntimeContext,
    RuntimeServices,
)


__all__ = (
    "AgentRunStream",
    "EngineRequest",
    "EngineResult",
    "EventType",
    "ExecutionEvent",
    "HookManager",
    "HookType",
    "RunStateError",
    "RunStateStore",
    "RunStateTransitionError",
    "RunStatus",
    "RunEventObserver",
    "RunObservationContext",
    "RunObservationFactory",
    "RuntimeContext",
    "RuntimeServices",
    "raw_text_delta",
    "reply_with_runtime",
    "resume_stream_with_runtime",
    "run_memory_daily_tick",
    "run_memory_idle_tick",
    "run_stream_with_runtime",
    "validate_execution_assets",
)
