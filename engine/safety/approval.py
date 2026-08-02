"""In-process approval broker for high-risk tool calls.

The broker deliberately carries only a redacted request summary across the
server boundary. The original tool call stays inside the suspended ReAct
frame and is executed only after the matching decision arrives.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from engine.safety.risk import RiskTier

# Substring parts, matched against a key stripped of non-alphanumerics, so
# ``db_password``/``clientSecret``/``auth_token`` redact just like ``password``.
# Exact full-name matching used to let every such variant through into the
# approval card and the SSE stream the user sees.
#
# Single source of truth: engine/safety/tool_guard.py imports the matching
# helper from here instead of keeping a second, drift-prone copy.
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


@dataclass(frozen=True)
class ApprovalScope:
    """One-time authority requested for an exact normalized tool call.

    The scope is deliberately descriptive rather than a reusable ACL.  The
    registry binds it to the call fingerprint and approval id before it reaches
    an execution environment.  This keeps an approved ``shell`` command from
    becoming a standing host-wide permission for a later model turn.
    """

    kind: str
    target: str
    access: tuple[str, ...]
    high_risk: bool = False

    @classmethod
    def host_command(cls, command: str, *, high_risk: bool = False) -> ApprovalScope:
        return cls(
            kind="host_command",
            target=command,
            access=("filesystem", "network", "process"),
            high_risk=high_risk,
        )

    @classmethod
    def path(
        cls,
        target: str,
        *,
        writing: bool,
        high_risk: bool = False,
    ) -> ApprovalScope:
        return cls(
            kind="path",
            target=target,
            access=(("read", "write") if writing else ("read",)),
            high_risk=high_risk,
        )

    @classmethod
    def network(cls, target: str, *, high_risk: bool = False) -> ApprovalScope:
        return cls(
            kind="network",
            target=target,
            access=("network",),
            high_risk=high_risk,
        )

    @classmethod
    def operation(
        cls,
        tool_name: str,
        *,
        high_risk: bool = False,
    ) -> ApprovalScope:
        return cls(
            kind="tool_operation",
            target=tool_name,
            access=("tool",),
            high_risk=high_risk,
        )

    @property
    def grants_host_execution(self) -> bool:
        return self.kind == "host_command"

    @property
    def description(self) -> str:
        if self.kind == "host_command":
            return "Host filesystem, network, and process access for this exact command"
        if self.kind == "network":
            return "Network access to the requested destination"
        if self.kind == "path":
            return f"{', '.join(self.access).capitalize()} access to the requested path"
        return "Access limited to this exact approved request"

    def to_dict(self) -> dict[str, object]:
        # Redact secrets in the serialized target: the host_command target is
        # the raw shell command and may carry tokens/keys that must not cross
        # the server boundary (approval matching still uses the unredacted
        # internal ``self.target``).
        return {
            "kind": self.kind,
            "target": _redact_secret_text(self.target),
            "access": list(self.access),
            "high_risk": self.high_risk,
        }

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
    scope: ApprovalScope | None = None
    presentation: ApprovalPresentation | None = None
    # Risk tier triaging this approval (see engine.safety.risk).  Carried so
    # the consumer can render/route the flow without re-deriving it.
    risk: "RiskTier | None" = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "approval_id": self.approval_id,
            "run_id": self.run_id,
            "tool": self.tool_name,
            "level": self.level,
            "reason": self.reason,
            "arguments": self.arguments_summary,
        }
        if self.risk is not None:
            payload["risk"] = self.risk.value
        if self.scope is not None:
            payload["scope"] = self.scope.to_dict()
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
    # Optional risk tier label so a consumer can render the card distinctly.
    risk: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "title": self.title,
            "summary": self.summary,
            "details": [detail.to_dict() for detail in self.details],
            "reason": self.reason,
        }
        if self.risk is not None:
            payload["risk"] = self.risk
        return payload


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


_SECRET_FLAG_RE = re.compile(
    r"^-{1,2}(?:password|passwd|token|secret|api[-_]?key|apikey|"
    r"access[-_]?key|client[-_]?secret|private[-_]?key|auth(?:orization)?|"
    r"bearer|session[-_]?key|cookie)(?:=|$)",
    re.IGNORECASE,
)

_SECRET_VALUE_RE = re.compile(
    r"(?:"
    r"sk-[A-Za-z0-9_\-]{8,}"
    r"|gh[pousr]_[A-Za-z0-9]{10,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[baprs]-[A-Za-z0-9\-]{8,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"
    r"|Bearer [A-Za-z0-9._\-]+"
    r")"
)

# Search-mode twin of ``_SECRET_VALUE_RE``: finds a credential embedded
# *inside* a longer string (a shell command, a URL, a JSON blob).  Lengths are
# tightened so ordinary content never matches — ``sk-development`` is a
# directory name, not a key.
_EMBEDDED_SECRET_RE = re.compile(
    r"(?:"
    r"sk-[A-Za-z0-9_\-]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{22,}"
    r"|glpat-[A-Za-z0-9_\-]{20,}"
    r"|xox[baprs]-[A-Za-z0-9\-]{16,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_\-]{30,}"
    r"|eyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{1,}\.[A-Za-z0-9_\-]{1,}"
    # A bare Bearer token that is not itself a recognized key family: keep the
    # word visible ("Bearer ***"), never the credential.  The negative
    # lookahead lets sk-/gh_/… tokens fall through to their own branch above so
    # the readable frame stays consistent with the other families.
    r"|\bBearer\s+(?!sk-|gh[pousr]_|github_pat_|glpat-|xox|AKIA|AIza|eyJ)"
    r"[A-Za-z0-9._~+/=\-]{8,}"
    r")"
)

# ``scheme://userinfo@`` in a URL — keep the scheme and host, drop the
# userinfo (which may carry a password).
_URL_CREDENTIALS_RE = re.compile(
    r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/@\s]+)@"
)

# ``--token value`` / ``--token=value`` embedded in a command string.
_EMBEDDED_FLAG_RE = re.compile(
    r"(?<=\s)(-{1,2}(?:password|passwd|token|secret|api[-_]?key|apikey|"
    r"access[-_]?key|client[-_]?secret|private[-_]?key|auth(?:orization)?|"
    r"bearer|session[-_]?key|cookie))(?:\s*=\s*|\s+)(?:(?:'[^']*')|(?:\"[^\"]*\")|[^\s;|&]+)",
    re.IGNORECASE,
)


def _is_secret_value(value: str) -> bool:
    """Whether a string is itself a credential rather than ordinary content."""
    text = value.strip()
    if not text:
        return False
    if re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", text):
        return True
    return bool(_SECRET_VALUE_RE.fullmatch(text))


def _redact_secret_text(text: str) -> str:
    """Redact secret-shaped material embedded in a string summary.

    A whole-string credential is hidden entirely; otherwise each embedded
    high-confidence token, ``--flag value`` pair, and URL userinfo is replaced
    in place so the surrounding context stays readable.
    """
    if _is_secret_value(text):
        return "***"
    redacted = _EMBEDDED_SECRET_RE.sub("***", text)
    redacted = _EMBEDDED_FLAG_RE.sub(r"\1 ***", redacted)
    redacted = _URL_CREDENTIALS_RE.sub(r"\1***@", redacted)
    return redacted


def _summarize_list(values: list | tuple, *, depth: int) -> list[object]:
    """Summarize a sequence, redacting flag-value pairs and secret-shaped values.

    Key-based redaction cannot see a credential that arrives as a positional
    list element (``["--token", "sk-..."]``); the only signal that the next
    element is sensitive is the flag that precedes it.  String elements that
    are themselves secret-shaped (or carry an embedded secret) are handled by
    ``_summarize_value`` -> ``_redact_secret_text``.
    """
    redacted: list[object] = []
    redact_next = False
    for item in values[:_MAX_SUMMARY_ITEMS]:
        if redact_next:
            redacted.append("***")
            redact_next = False
            continue
        if isinstance(item, str):
            match = _SECRET_FLAG_RE.match(item)
            if match:
                if "=" in item:
                    # ``--token=sk-...`` carries the value inline; keep the flag
                    # name visible and drop only the value.
                    redacted.append(item.split("=", 1)[0] + "=***")
                else:
                    redacted.append(item)
                    redact_next = True
                continue
        redacted.append(_summarize_value(item, depth=depth + 1))
    return redacted


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
        safe = _redact_secret_text(safe)
        return safe[:_MAX_SUMMARY_TEXT] + ("…" if len(safe) > _MAX_SUMMARY_TEXT else "")
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if depth >= _MAX_SUMMARY_DEPTH:
        return "[nested value omitted]"
    if isinstance(value, dict):
        return _summarize_mapping(value, depth=depth + 1)
    if isinstance(value, (list, tuple)):
        return _summarize_list(value, depth=depth)
    return _summarize_value(str(value), depth=depth + 1)


def _display_value(value: object, *, max_length: int = _MAX_SUMMARY_TEXT) -> str:
    if isinstance(value, str):
        text = _redact_secret_text(value)
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(value)
        text = _redact_secret_text(text)
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


def _approval_details(
    tool_name: str,
    arguments: dict[str, object],
    *,
    scope: ApprovalScope | None = None,
) -> tuple[ApprovalDetail, ...]:
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
    if scope is not None:
        details.append(ApprovalDetail(label="Access scope", value=scope.description))
    return tuple(details)


def build_approval_presentation(
    tool_name: str,
    level: str,
    reason: str,
    arguments: dict[str, object],
    *,
    tool_description: str = "",
    scope: ApprovalScope | None = None,
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
        details=_approval_details(tool_name, arguments, scope=scope),
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
