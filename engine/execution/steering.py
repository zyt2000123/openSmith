"""In-process mid-turn steering mailbox for active Agent runs.

Follows the same Broker + ContextVar isolation pattern as
``engine.safety.approval`` so the execution engine remains completely unaware
of FastAPI/HTTP while allowing callers to inject user course-corrections into
an active run between ReAct iterations.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from engine.context.compression import STEERING_USER_NOTE_PREFIX

MAX_STEERING_QUEUE_ITEMS = 8
MAX_STEERING_TOTAL_CHARS = 16_000
MAX_STEERING_CONTINUATIONS = 3

SteerStatus = Literal[
    "injected",
    "queued_while_waiting_approval",
    "no_active_run",
    "run_mismatch",
    "inbox_full",
]


class SteeringInboxClosedError(RuntimeError):
    """Raised when attempting to push to a closed steering inbox."""


class SteeringInboxFullError(RuntimeError):
    """Raised when a steering inbox exceeds its item or character cap."""


@dataclass(frozen=True)
class SteeringMessage:
    steer_id: str
    content: str
    created_at: str


@dataclass(frozen=True)
class SteerSubmitResult:
    accepted: bool
    status: SteerStatus
    run_id: str | None = None
    steer_id: str | None = None
    pending_count: int = 0


def format_steering_message(items: Sequence[SteeringMessage]) -> str:
    """Fold one or more mid-turn steering notes into a single user message."""
    header = (
        f"{STEERING_USER_NOTE_PREFIX}"
        "用户在本轮执行过程中补充了以下纠偏指令，请在不推翻已验证事实的前提下调整后续步骤："
    )
    lines = [header]
    for idx, item in enumerate(items, 1):
        lines.append(f"{idx}. {item.content}")
    return "\n".join(lines)


class SteeringInbox:
    """Thread-safe per-run mailbox for mid-turn user steering messages."""

    def __init__(
        self,
        *,
        max_items: int = MAX_STEERING_QUEUE_ITEMS,
        max_chars: int = MAX_STEERING_TOTAL_CHARS,
    ) -> None:
        self._lock = threading.Lock()
        self._items: list[SteeringMessage] = []
        self._total_chars = 0
        self._closed = False
        self._max_items = max_items
        self._max_chars = max_chars

    def push(self, content: str) -> SteeringMessage:
        normalized = content.strip()
        if not normalized:
            raise ValueError("steering content must not be empty")
        with self._lock:
            if self._closed:
                raise SteeringInboxClosedError("steering inbox is closed")
            if (
                len(self._items) >= self._max_items
                or self._total_chars + len(normalized) > self._max_chars
            ):
                raise SteeringInboxFullError("steering inbox is full")
            msg = SteeringMessage(
                steer_id=f"st_{uuid4().hex[:12]}",
                content=normalized,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            self._items.append(msg)
            self._total_chars += len(normalized)
            return msg

    def drain(self) -> list[SteeringMessage]:
        with self._lock:
            if not self._items:
                return []
            drained = list(self._items)
            self._items.clear()
            self._total_chars = 0
            return drained

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._items)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._items)

    def close(self) -> list[SteeringMessage]:
        with self._lock:
            if self._closed:
                return []
            self._closed = True
            remainder = list(self._items)
            self._items.clear()
            self._total_chars = 0
            return remainder


class SteeringBroker:
    """Process-wide registry of active run steering inboxes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_run: dict[str, tuple[str | None, SteeringInbox]] = {}
        self._active_by_session: dict[str, str] = {}

    def open_run(self, run_id: str, session_id: str | None = None) -> SteeringInbox:
        inbox = SteeringInbox()
        with self._lock:
            self._by_run[run_id] = (session_id, inbox)
            if session_id:
                self._active_by_session[session_id] = run_id
        return inbox

    def close_run(self, run_id: str) -> list[SteeringMessage]:
        with self._lock:
            entry = self._by_run.pop(run_id, None)
            if entry is None:
                return []
            session_id, inbox = entry
            if session_id and self._active_by_session.get(session_id) == run_id:
                self._active_by_session.pop(session_id, None)
        return inbox.close()

    def active_run_id(self, session_id: str) -> str | None:
        with self._lock:
            return self._active_by_session.get(session_id)

    def submit(
        self,
        *,
        session_id: str,
        content: str,
        run_id: str | None = None,
        waiting_approval: bool = False,
    ) -> SteerSubmitResult:
        with self._lock:
            active_id = self._active_by_session.get(session_id)
            if active_id is None:
                return SteerSubmitResult(accepted=False, status="no_active_run")
            if run_id is not None and run_id != active_id:
                return SteerSubmitResult(
                    accepted=False,
                    status="run_mismatch",
                    run_id=active_id,
                )
            entry = self._by_run.get(active_id)
            if entry is None:
                return SteerSubmitResult(accepted=False, status="no_active_run")
            _, inbox = entry

        try:
            msg = inbox.push(content)
        except SteeringInboxClosedError:
            return SteerSubmitResult(accepted=False, status="no_active_run")
        except SteeringInboxFullError:
            return SteerSubmitResult(
                accepted=False,
                status="inbox_full",
                run_id=active_id,
                pending_count=inbox.pending_count(),
            )

        status: SteerStatus = (
            "queued_while_waiting_approval" if waiting_approval else "injected"
        )
        return SteerSubmitResult(
            accepted=True,
            status=status,
            run_id=active_id,
            steer_id=msg.steer_id,
            pending_count=inbox.pending_count(),
        )


STEERING_BROKER = SteeringBroker()

_ACTIVE_STEERING_INBOX: ContextVar[SteeringInbox | None] = ContextVar(
    "agent_smith_steering_inbox",
    default=None,
)


def current_steering_inbox() -> SteeringInbox | None:
    return _ACTIVE_STEERING_INBOX.get()


@contextmanager
def use_steering_inbox(inbox: SteeringInbox | None) -> Iterator[None]:
    token = _ACTIVE_STEERING_INBOX.set(inbox)
    try:
        yield
    finally:
        _ACTIVE_STEERING_INBOX.reset(token)


@contextmanager
def without_steering_inbox() -> Iterator[None]:
    """Detach from any active parent steering inbox (used by sub-agents)."""
    token = _ACTIVE_STEERING_INBOX.set(None)
    try:
        yield
    finally:
        _ACTIVE_STEERING_INBOX.reset(token)


__all__ = (
    "MAX_STEERING_CONTINUATIONS",
    "MAX_STEERING_QUEUE_ITEMS",
    "MAX_STEERING_TOTAL_CHARS",
    "STEERING_BROKER",
    "STEERING_USER_NOTE_PREFIX",
    "SteerStatus",
    "SteerSubmitResult",
    "SteeringBroker",
    "SteeringInbox",
    "SteeringInboxClosedError",
    "SteeringInboxFullError",
    "SteeringMessage",
    "current_steering_inbox",
    "format_steering_message",
    "use_steering_inbox",
    "without_steering_inbox",
)
