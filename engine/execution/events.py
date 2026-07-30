"""Typed events emitted by the Engine execution Interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol

from engine.llm.events import ProviderEventType


class EventType(str, Enum):
    """All event types emitted during one Agent execution."""

    RUN_STARTED = "run_started"
    RAW_RESPONSE_EVENT = "raw_response_event"
    THINKING = "thinking"
    TEXT_DELTA = "text_delta"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_RESULT = "tool_call_result"
    SKILL_START = "skill_start"
    SKILL_END = "skill_end"
    GATE_RESULT = "gate_result"
    ROUTE_DECIDED = "route_decided"
    BACKTRACK = "backtrack"
    BLOCKED = "blocked"
    AWAITING_INPUT = "awaiting_input"
    TOKEN_USAGE = "token_usage"
    CONTEXT_USAGE = "context_usage"
    CONTEXT_COMPRESSION_START = "context_compression_start"
    CONTEXT_COMPRESSION_END = "context_compression_end"
    PROVISIONAL_TEXT_DELTA = "provisional_text_delta"
    PROVISIONAL_COMMIT = "provisional_commit"
    PROVISIONAL_RETRACT = "provisional_retract"
    SMITH_UI = "smith_ui"
    SMITH_UI_FALLBACK = "smith_ui_fallback"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    DONE = "done"
    RUN_FINISHED = "run_finished"


@dataclass
class ExecutionEvent:
    """One event emitted during an Agent execution."""

    type: EventType
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a transport-safe dictionary."""
        return {"type": self.type.value, "data": self.data}


@dataclass(frozen=True)
class RunObservationContext:
    """Non-sensitive context supplied to a run-observer Adapter."""

    run_id: str
    agent_id: str
    profile_dir: Path
    session_id: str | None = None
    identity_id: str | None = None
    working_dir: str | None = None
    forced_skill: str | None = None
    created_at: str | None = None


class RunEventObserver(Protocol):
    """Execution-owned Interface implemented by observability Adapters."""

    def record(self, event: ExecutionEvent) -> None:
        """Consume one execution event."""

    def append_prompt_manifest(self, manifest: dict[str, object]) -> None:
        """Consume prompt provenance for the run."""


RunObservationFactory = Callable[[RunObservationContext], RunEventObserver]


def raw_text_delta(
    event: ExecutionEvent,
    *,
    include_provisional: bool = True,
) -> str | None:
    """Extract normalized provider text from a raw execution event."""
    event_type = getattr(event.type, "value", event.type)
    if event_type != EventType.RAW_RESPONSE_EVENT.value:
        return None
    if not include_provisional and event.data.get("provision_id"):
        return None
    if event.data.get("type") != ProviderEventType.OUTPUT_TEXT_DELTA.value:
        return None
    raw_data = event.data.get("data")
    if not isinstance(raw_data, dict):
        return None
    text = raw_data.get("delta")
    return text if isinstance(text, str) and text else None
