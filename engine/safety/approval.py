"""In-process approval broker for high-risk tool calls.

The broker deliberately carries only a redacted request summary across the
server boundary. The original tool call stays inside the suspended ReAct
frame and is executed only after the matching decision arrives.
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


# Substring parts, matched against a key stripped of non-alphanumerics, so
# ``db_password``/``clientSecret``/``auth_token`` redact just like ``password``.
# Exact full-name matching used to let every such variant through into the
# approval card and the SSE stream the user sees.
#
# Keep in sync with ``_SENSITIVE_ARG_KEY_PARTS`` in engine/safety/tool_guard.py —
# that module imports this one, so the shared helper cannot live over there.
_SENSITIVE_ARGUMENT_KEY_PARTS = (
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "passwd",
    "password",
    "privatekey",
    "secret",
    "token",
)


def _is_sensitive_argument_name(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return any(part in normalized for part in _SENSITIVE_ARGUMENT_KEY_PARTS)
_MAX_SUMMARY_ITEMS = 32
_MAX_SUMMARY_DEPTH = 3
_MAX_SUMMARY_TEXT = 240
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 300.0


class ApprovalTimeoutError(TimeoutError):
    """Raised when a run waits too long for an approval decision."""

_DETAIL_LABELS = {
    "action": "Action",
    "append": "Append",
    "branch": "Branch",
    "command": "Command",
    "content": "Content preview",
    "cwd": "Working directory",
    "file_path": "File",
    "files": "Files",
    "agent_id": "Agent",
    "episode_id": "Episode",
    "evidence": "Evidence",
    "environment": "Environment",
    "index": "Task index",
    "message": "Commit message",
    "new_string": "Replacement text",
    "old_string": "Text to replace",
    "path": "Path",
    "query": "Query",
    "replace_all": "Replace all",
    "section": "Section",
    "section_content": "Section content",
    "staged": "Staged only",
    "status": "Status",
    "text": "Task text",
    "timeout": "Timeout (seconds)",
    "topic": "Topic",
    "version_id": "Version",
}
_DETAIL_ORDER = {
    "shell": ("command", "cwd", "timeout"),
    "write_file": ("path", "append", "content"),
    "edit_file": ("path", "old_string", "new_string", "replace_all"),
    "git_ops": ("action", "cwd", "branch", "message", "files", "path", "staged"),
}
_GIT_ACTIONS = {
    "branch_create": ("Create a Git branch", "Create a new branch"),
    "commit": ("Commit Git changes", "Create a Git commit"),
    "push": ("Push Git changes", "Push commits to a remote branch"),
    "worktree_create": ("Create a Git worktree", "Create an additional working tree"),
    "worktree_remove": ("Remove a Git worktree", "Delete an additional working tree"),
}
_STRUCTURED_ACTIONS = {
    "memory_ops": {
        "add": ("Add a memory", "Save a new memory event", "This changes persistent agent memory."),
        "episode": ("Create a memory episode", "Archive the requested memory episode", "This changes persistent agent memory."),
        "remove": ("Remove a memory", "Remove the requested memory episode", "This changes persistent agent memory."),
        "update": ("Update a memory", "Update the requested memory episode", "This changes persistent agent memory."),
    },
    "skill_manage": {
        "create": ("Create an agent skill", "Create a new installed skill", "This changes agent instructions."),
        "edit": ("Edit an agent skill", "Update an installed skill", "This changes agent instructions."),
        "patch": ("Patch an agent skill", "Update a section of an installed skill", "This changes agent instructions."),
        "rollback": ("Rollback an agent skill", "Restore an earlier skill version", "This changes agent instructions."),
    },
    "todo": {
        "add": ("Add a task", "Add a task to the session list", "This changes the session task list."),
        "clear": ("Clear tasks", "Remove all tasks from the session list", "This changes the session task list."),
        "remove": ("Remove a task", "Remove a task from the session list", "This changes the session task list."),
        "update": ("Update a task", "Change a task in the session list", "This changes the session task list."),
    },
}


@dataclass(frozen=True)
class ApprovalRequest:
    approval_id: str
    run_id: str
    tool_name: str
    level: str
    reason: str
    arguments_summary: dict[str, object]
    presentation: "ApprovalPresentation | None" = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "approval_id": self.approval_id,
            "run_id": self.run_id,
            "tool": self.tool_name,
            "level": self.level,
            "reason": self.reason,
            "arguments": self.arguments_summary,
        }
        if self.presentation is not None:
            payload["presentation"] = self.presentation.to_dict()
        return payload


@dataclass(frozen=True)
class ApprovalDetail:
    label: str
    value: str

    def to_dict(self) -> dict[str, str]:
        return {"label": self.label, "value": self.value}


@dataclass(frozen=True)
class ApprovalPresentation:
    title: str
    summary: str
    details: tuple[ApprovalDetail, ...]
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "summary": self.summary,
            "details": [detail.to_dict() for detail in self.details],
            "reason": self.reason,
        }


@dataclass
class _PendingApproval:
    request: ApprovalRequest
    event: asyncio.Event
    decision: bool | None = None


class ApprovalBroker:
    """Coordinate one live approval decision without re-running the model."""

    def __init__(self) -> None:
        self._pending: dict[str, _PendingApproval] = {}

    def open(self, request: ApprovalRequest) -> ApprovalRequest:
        if request.approval_id in self._pending:
            raise ValueError(f"Approval request already exists: {request.approval_id}")
        self._pending[request.approval_id] = _PendingApproval(
            request=request,
            event=asyncio.Event(),
        )
        return request

    async def wait(
        self,
        request: ApprovalRequest,
        *,
        timeout_seconds: float | None = DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    ) -> bool:
        pending = self._pending.get(request.approval_id)
        if pending is None or pending.request.run_id != request.run_id:
            raise RuntimeError("Approval request is no longer active")
        try:
            if timeout_seconds is None:
                await pending.event.wait()
            else:
                await asyncio.wait_for(pending.event.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            self._pending.pop(request.approval_id, None)
            raise ApprovalTimeoutError(
                f"Approval timed out after {timeout_seconds:g} seconds"
            ) from exc
        except asyncio.CancelledError:
            self._pending.pop(request.approval_id, None)
            raise
        decision = pending.decision
        self._pending.pop(request.approval_id, None)
        return decision is True

    def is_pending(self, run_id: str, approval_id: str) -> bool:
        pending = self._pending.get(approval_id)
        return pending is not None and not pending.event.is_set() and pending.request.run_id == run_id

    def resolve(self, run_id: str, approval_id: str, approved: bool) -> bool:
        pending = self._pending.get(approval_id)
        if pending is None or pending.event.is_set() or pending.request.run_id != run_id:
            return False
        pending.decision = bool(approved)
        pending.event.set()
        return True

    def cancel_run(self, run_id: str) -> None:
        for approval_id, pending in list(self._pending.items()):
            if pending.request.run_id != run_id:
                continue
            pending.decision = False
            pending.event.set()
            self._pending.pop(approval_id, None)


def summarize_arguments(arguments: dict) -> dict[str, object]:
    """Return a bounded, redacted summary safe for SSE and run metadata."""
    return _summarize_mapping(arguments)


def _summarize_mapping(arguments: dict, *, depth: int = 0) -> dict[str, object]:
    summary: dict[str, object] = {}
    for index, (raw_key, value) in enumerate(arguments.items()):
        if index >= _MAX_SUMMARY_ITEMS:
            summary["…"] = "truncated"
            break
        key = str(raw_key)[:80]
        summary[key] = _summarize_value(value, key=key, depth=depth)
    return summary


def _summarize_value(value: object, *, key: str | None = None, depth: int = 0) -> object:
    if key is not None and _is_sensitive_argument_name(key):
        return "***"
    if isinstance(value, str):
        safe = "".join(char if ord(char) >= 32 and char != "\x7f" else " " for char in value)
        return safe[:_MAX_SUMMARY_TEXT] + ("…" if len(safe) > _MAX_SUMMARY_TEXT else "")
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if depth >= _MAX_SUMMARY_DEPTH:
        return "[nested value omitted]"
    if isinstance(value, dict):
        return _summarize_mapping(value, depth=depth + 1)
    if isinstance(value, (list, tuple)):
        return [
            _summarize_value(item, depth=depth + 1)
            for item in value[:_MAX_SUMMARY_ITEMS]
        ]
    return _summarize_value(str(value), depth=depth + 1)


def _display_value(value: object, *, max_length: int = _MAX_SUMMARY_TEXT) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(value)
    compact = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    return compact[:max_length] + ("…" if len(compact) > max_length else "")


def _humanize_name(value: str) -> str:
    return " ".join(value.replace("_", " ").replace("-", " ").split()).capitalize()


def _specific_reason(reason: str, fallback: str) -> str:
    compact = _display_value(reason)
    lowered = compact.lower()
    if compact and lowered not in {"approval required", "approval required for"} and not lowered.startswith("approval required for "):
        return compact
    return fallback


def _approval_details(tool_name: str, arguments: dict[str, object]) -> tuple[ApprovalDetail, ...]:
    order = {key: index for index, key in enumerate(_DETAIL_ORDER.get(tool_name, ()))}
    entries = [
        (key, value)
        for key, value in arguments.items()
        if value is not None and value != ""
    ]
    entries.sort(key=lambda item: (order.get(item[0], len(order)), item[0]))
    details: list[ApprovalDetail] = []
    for key, value in entries:
        label = _DETAIL_LABELS.get(key, _humanize_name(key))
        details.append(ApprovalDetail(label=label, value=_display_value(value)))
    return tuple(details)


def build_approval_presentation(
    tool_name: str,
    level: str,
    reason: str,
    arguments: dict[str, object],
    *,
    tool_description: str = "",
) -> ApprovalPresentation:
    """Build a user-facing description from safe, bounded tool arguments."""
    action = str(arguments.get("action") or "").strip().lower()
    target = _display_value(arguments.get("path") or arguments.get("file_path") or "")

    if tool_name == "shell":
        title = "Run a shell command"
        summary = "Execute the requested command"
        fallback_reason = "This command may change files or system state."
    elif tool_name == "write_file":
        title = "Write a file"
        summary = f"{'Append to' if arguments.get('append') else 'Write to'} {target or 'the requested path'}"
        fallback_reason = "This will change file contents."
    elif tool_name == "edit_file":
        title = "Edit a file"
        summary = f"Replace text in {target or 'the requested path'}"
        fallback_reason = "This will change file contents."
    elif tool_name == "git_ops":
        title, summary = _GIT_ACTIONS.get(
            action,
            ("Perform a Git operation", f"Perform the requested Git operation{f' ({action})' if action else ''}"),
        )
        fallback_reason = "This will change repository state or communicate with a Git remote."
    elif tool_name in _STRUCTURED_ACTIONS:
        action_presentation = _STRUCTURED_ACTIONS[tool_name].get(action)
        subject = _display_value(
            arguments.get("skill_name") or arguments.get("episode_id") or arguments.get("topic") or ""
        )
        if action_presentation is not None:
            title, summary, fallback_reason = action_presentation
            if subject and tool_name == "skill_manage":
                summary = f"{summary}: {subject}"
        else:
            title = f"Manage {_humanize_name(tool_name)}"
            summary = f"Perform the requested {action or 'operation'}"
            fallback_reason = "This operation changes persistent agent state."
    else:
        title = f"Use {_humanize_name(tool_name) or 'a tool'}"
        summary = _display_value(tool_description) or f"Execute {_humanize_name(tool_name) or 'the requested tool'}"
        fallback_reason = "This operation requires user approval."

    return ApprovalPresentation(
        title=title,
        summary=summary,
        details=_approval_details(tool_name, arguments),
        reason=_specific_reason(reason, fallback_reason),
    )


APPROVAL_BROKER = ApprovalBroker()
_CURRENT_APPROVAL_CONTEXT: ContextVar[tuple[ApprovalBroker, str] | None] = ContextVar(
    "agent_smith_approval_context",
    default=None,
)


@contextmanager
def use_approval_context(broker: ApprovalBroker, run_id: str) -> Iterator[None]:
    token = _CURRENT_APPROVAL_CONTEXT.set((broker, run_id))
    try:
        yield
    finally:
        _CURRENT_APPROVAL_CONTEXT.reset(token)


def current_approval_context() -> tuple[ApprovalBroker, str] | None:
    return _CURRENT_APPROVAL_CONTEXT.get()
