"""Durable lifecycle state for one Agent execution.

RunState is intentionally smaller than a conversation/session snapshot.  It
tracks where an execution is in its lifecycle and a few bounded progress
fields, but it does not persist model messages or raw tool arguments.  That
keeps the state safe to expose for status polling and avoids accidental
re-execution of side-effectful tools during a future resume flow.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from uuid import uuid4

from common.paths import PRIVATE_DIR_MODE, PRIVATE_FILE_MODE
from engine.observability.events import EventType, ExecutionEvent


class RunStatus(str, Enum):
    """Lifecycle states that can be observed for one execution."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunStateError(RuntimeError):
    """Base error for invalid or unreadable persisted run state."""


class RunStateTransitionError(RunStateError):
    """Raised when a run tries to skip or leave an invalid lifecycle state."""


_ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({
        RunStatus.RUNNING,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }),
    RunStatus.RUNNING: frozenset({
        RunStatus.RUNNING,
        RunStatus.WAITING_APPROVAL,
        RunStatus.COMPLETED,
        RunStatus.INCOMPLETE,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }),
    RunStatus.WAITING_APPROVAL: frozenset({
        RunStatus.RUNNING,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }),
    RunStatus.COMPLETED: frozenset({RunStatus.COMPLETED}),
    RunStatus.INCOMPLETE: frozenset({RunStatus.INCOMPLETE, RunStatus.RUNNING}),
    RunStatus.FAILED: frozenset({RunStatus.FAILED, RunStatus.RUNNING}),
    RunStatus.CANCELLED: frozenset({RunStatus.CANCELLED, RunStatus.RUNNING}),
}

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_text(value: object | None, *, limit: int = 200) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:limit] or None


@dataclass
class RunState:
    """Persisted metadata for one execution attempt."""

    run_id: str
    agent_id: str
    session_id: str | None = None
    message_id: str | None = None
    identity_id: str | None = None
    working_dir: str | None = None
    forced_skill: str | None = None
    status: RunStatus = RunStatus.QUEUED
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    event_seq: int = 0
    last_event_type: str | None = None
    current_skill: str | None = None
    current_tool: str | None = None
    reason: str | None = None
    error: str | None = None
    approval_id: str | None = None
    approval_tool: str | None = None
    approval_level: str | None = None
    approval_reason: str | None = None

    def transition(
        self,
        status: RunStatus | str,
        *,
        reason: str | None = None,
        error: str | None = None,
    ) -> None:
        target = RunStatus(status)
        if target not in _ALLOWED_TRANSITIONS[self.status]:
            raise RunStateTransitionError(
                f"Cannot transition run {self.run_id!r} from "
                f"{self.status.value!r} to {target.value!r}"
            )
        self.status = target
        if reason is not None:
            self.reason = _bounded_text(reason)
        if error is not None:
            self.error = _bounded_text(error)
        self.updated_at = _now()

    def record_event(
        self,
        event_type: str,
        *,
        current_skill: str | None = None,
        current_tool: str | None = None,
        clear_skill: bool = False,
        clear_tool: bool = False,
    ) -> None:
        self.event_seq += 1
        self.last_event_type = _bounded_text(event_type)
        if current_skill is not None:
            self.current_skill = _bounded_text(current_skill)
        elif clear_skill:
            self.current_skill = None
        if current_tool is not None:
            self.current_tool = _bounded_text(current_tool)
        elif clear_tool:
            self.current_tool = None
        self.updated_at = _now()

    def to_dict(self) -> dict[str, object | None]:
        return {
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "message_id": self.message_id,
            "identity_id": self.identity_id,
            "working_dir": self.working_dir,
            "forced_skill": self.forced_skill,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "event_seq": self.event_seq,
            "last_event_type": self.last_event_type,
            "current_skill": self.current_skill,
            "current_tool": self.current_tool,
            "reason": self.reason,
            "error": self.error,
            "approval_id": self.approval_id,
            "approval_tool": self.approval_tool,
            "approval_level": self.approval_level,
            "approval_reason": self.approval_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "RunState":
        try:
            run_id = str(data["run_id"])
            agent_id = str(data["agent_id"])
            status = RunStatus(str(data["status"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RunStateError("Invalid persisted run state") from exc

        event_seq = data.get("event_seq", 0)
        try:
            parsed_event_seq = max(0, int(event_seq))
        except (TypeError, ValueError) as exc:
            raise RunStateError("Invalid persisted run event sequence") from exc

        return cls(
            run_id=run_id,
            agent_id=agent_id,
            session_id=_bounded_text(data.get("session_id")),
            message_id=_bounded_text(data.get("message_id")),
            identity_id=_bounded_text(data.get("identity_id")),
            working_dir=_bounded_text(data.get("working_dir"), limit=1024),
            forced_skill=_bounded_text(data.get("forced_skill")),
            status=status,
            created_at=str(data.get("created_at") or _now()),
            updated_at=str(data.get("updated_at") or _now()),
            event_seq=parsed_event_seq,
            last_event_type=_bounded_text(data.get("last_event_type")),
            current_skill=_bounded_text(data.get("current_skill")),
            current_tool=_bounded_text(data.get("current_tool")),
            reason=_bounded_text(data.get("reason")),
            error=_bounded_text(data.get("error")),
            approval_id=_bounded_text(data.get("approval_id")),
            approval_tool=_bounded_text(data.get("approval_tool")),
            approval_level=_bounded_text(data.get("approval_level")),
            approval_reason=_bounded_text(data.get("approval_reason")),
        )


class RunStateStore:
    """Atomic, private JSON persistence for run metadata."""

    def __init__(self, profile_dir: Path) -> None:
        self.root = Path(profile_dir) / "runs"
        self.root.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
        self.root.chmod(PRIVATE_DIR_MODE)

    @staticmethod
    def _validate_run_id(run_id: str) -> str:
        if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
            raise ValueError("invalid run id")
        return run_id

    def _path(self, run_id: str) -> Path:
        return self.root / f"{self._validate_run_id(run_id)}.json"

    def create(
        self,
        run_id: str,
        *,
        agent_id: str,
        session_id: str | None = None,
        message_id: str | None = None,
        identity_id: str | None = None,
        working_dir: str | None = None,
        forced_skill: str | None = None,
    ) -> RunState:
        path = self._path(run_id)
        if path.exists():
            raise RunStateError(f"Run state already exists for {run_id!r}")
        state = RunState(
            run_id=run_id,
            agent_id=_bounded_text(agent_id) or "unknown",
            session_id=_bounded_text(session_id),
            message_id=_bounded_text(message_id),
            identity_id=_bounded_text(identity_id),
            working_dir=_bounded_text(working_dir, limit=1024),
            forced_skill=_bounded_text(forced_skill),
        )
        self.save(state)
        return state

    def resume(self, run_id: str) -> RunState:
        """Resume a recoverable run without allowing completed work to rerun."""
        state = self._require(run_id)
        if state.status not in {
            RunStatus.INCOMPLETE,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            raise RunStateTransitionError(
                f"Run {run_id!r} is not resumable from {state.status.value!r}"
            )
        state.transition(RunStatus.RUNNING, reason="resumed")
        state.record_event("run_resumed")
        self.save(state)
        return state

    def request_approval(
        self,
        run_id: str,
        *,
        approval_id: str,
        tool_name: str,
        level: str,
        reason: str,
    ) -> RunState:
        state = self._require(run_id)
        if state.status is not RunStatus.RUNNING:
            raise RunStateTransitionError(
                f"Run {run_id!r} cannot request approval from {state.status.value!r}"
            )
        state.approval_id = _bounded_text(approval_id)
        state.approval_tool = _bounded_text(tool_name)
        state.approval_level = _bounded_text(level)
        state.approval_reason = _bounded_text(reason)
        state.record_event("approval_required", clear_tool=True)
        state.transition(RunStatus.WAITING_APPROVAL, reason=reason)
        self.save(state)
        return state

    def resolve_approval(
        self,
        run_id: str,
        approval_id: str,
        *,
        approved: bool,
        event_type: str | None = None,
        reason: str | None = None,
    ) -> RunState:
        """Clear a pending approval and return the run to its execution state.

        ``event_type`` and ``reason`` let the engine record automatic
        resolutions such as an approval timeout without pretending that the
        user explicitly denied the request.
        """
        state = self._require(run_id)
        if state.status is not RunStatus.WAITING_APPROVAL:
            raise RunStateTransitionError(
                f"Run {run_id!r} is not waiting for approval"
            )
        if state.approval_id != approval_id:
            raise RunStateError("Approval request does not match the pending run")
        state.approval_id = None
        state.approval_tool = None
        state.approval_level = None
        state.approval_reason = None
        resolution = "approval_granted" if approved else "approval_denied"
        state.record_event(event_type or resolution)
        state.transition(
            RunStatus.RUNNING,
            reason=reason or resolution,
        )
        self.save(state)
        return state

    def recover_interrupted(self) -> list[str]:
        """Mark runs left active by a previous server process as resumable.

        A live approval continuation only exists in that process's memory, so
        neither a queued/running run nor a waiting approval can safely remain
        active after startup.  ``CANCELLED`` is intentionally resumable and
        preserves the existing ledger-based replay safeguards.
        """
        recovered: list[str] = []
        for path in sorted(self.root.glob("*.json")):
            run_id = path.stem
            if not _RUN_ID_RE.fullmatch(run_id):
                continue
            state = self.get(run_id)
            if state is None or state.status not in {
                RunStatus.QUEUED,
                RunStatus.RUNNING,
                RunStatus.WAITING_APPROVAL,
            }:
                continue
            state.approval_id = None
            state.approval_tool = None
            state.approval_level = None
            state.approval_reason = None
            state.record_event("run_interrupted", clear_skill=True, clear_tool=True)
            state.transition(RunStatus.CANCELLED, reason="server_restarted")
            self.save(state)
            recovered.append(run_id)
        return recovered

    def get(self, run_id: str) -> RunState | None:
        path = self._path(run_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunStateError(f"Unable to read run state for {run_id!r}") from exc
        if not isinstance(data, dict):
            raise RunStateError(f"Invalid run state payload for {run_id!r}")
        state = RunState.from_dict(data)
        if state.run_id != run_id:
            raise RunStateError(f"Run state id mismatch for {run_id!r}")
        return state

    def save(self, state: RunState) -> None:
        path = self._path(state.run_id)
        payload = json.dumps(
            state.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        temp_path = self.root / f".{state.run_id}.{uuid4().hex}.tmp"
        try:
            fd = os.open(
                temp_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                PRIVATE_FILE_MODE,
            )
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            path.chmod(PRIVATE_FILE_MODE)
        except OSError as exc:
            temp_path.unlink(missing_ok=True)
            raise RunStateError(f"Unable to save run state for {state.run_id!r}") from exc

    def transition(
        self,
        run_id: str,
        status: RunStatus | str,
        *,
        event_type: str | None = None,
        reason: str | None = None,
        error: str | None = None,
    ) -> RunState:
        state = self._require(run_id)
        if event_type is not None:
            state.record_event(event_type)
        state.transition(status, reason=reason, error=error)
        self.save(state)
        return state

    def record_event(
        self,
        run_id: str,
        event_type: str,
        *,
        current_skill: str | None = None,
        current_tool: str | None = None,
        clear_skill: bool = False,
        clear_tool: bool = False,
    ) -> RunState:
        state = self._require(run_id)
        state.record_event(
            event_type,
            current_skill=current_skill,
            current_tool=current_tool,
            clear_skill=clear_skill,
            clear_tool=clear_tool,
        )
        self.save(state)
        return state

    def _require(self, run_id: str) -> RunState:
        state = self.get(run_id)
        if state is None:
            raise RunStateError(f"Run state not found for {run_id!r}")
        return state


def project_execution_event(
    store: RunStateStore | None,
    run_id: str,
    event: ExecutionEvent,
) -> None:
    """Project an execution event into durable runtime-control state.

    This belongs to the execution control plane rather than observability:
    the state determines whether a run can be resumed or needs approval.  It
    is deliberately best-effort so a corrupt state file never interrupts an
    otherwise valid run.
    """
    if store is None:
        return
    try:
        event_type = event.type.value
        if event.type is EventType.RUN_STARTED:
            store.transition(run_id, RunStatus.RUNNING, event_type=event_type)
            return
        if event.type is EventType.RUN_FINISHED:
            status = RunStatus(str(event.data.get("status", RunStatus.FAILED.value)))
            reason = event.data.get("reason")
            store.transition(
                run_id,
                status,
                event_type=event_type,
                reason=str(reason) if reason is not None else None,
                error=str(reason) if status is RunStatus.FAILED and reason is not None else None,
            )
            return

        kwargs: dict[str, object] = {}
        if event.type is EventType.SKILL_START:
            kwargs["current_skill"] = event.data.get("skill")
        elif event.type is EventType.SKILL_END:
            kwargs["clear_skill"] = True
        elif event.type is EventType.TOOL_CALL_START:
            kwargs["current_tool"] = event.data.get("name")
        elif event.type is EventType.TOOL_CALL_RESULT:
            if event.data.get("approval_required"):
                store.request_approval(
                    run_id,
                    approval_id=str(event.data.get("approval_id") or ""),
                    tool_name=str(event.data.get("tool") or "tool"),
                    level=str(event.data.get("level") or "execute"),
                    reason=str(event.data.get("reason") or "Approval required"),
                )
                return
            approval_outcome = str(event.data.get("approval_outcome") or "")
            if approval_outcome in {"denied", "timed_out"}:
                state = store.get(run_id)
                if state is not None and state.status is RunStatus.WAITING_APPROVAL:
                    approval_id = str(event.data.get("approval_id") or "")
                    store.resolve_approval(
                        run_id,
                        approval_id,
                        approved=False,
                        event_type=event_type,
                        reason=f"approval_{approval_outcome}",
                    )
                    return
            kwargs["clear_tool"] = True
        store.record_event(run_id, event_type, **kwargs)
    except (RunStateError, ValueError, TypeError):
        # Run control state must not take down an otherwise valid execution.
        import logging

        logging.getLogger(__name__).warning(
            "failed to project run state event (run=%s, event=%s)",
            run_id,
            event.type.value,
            exc_info=True,
        )
