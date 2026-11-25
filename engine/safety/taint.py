"""Session-scoped taint bookkeeping and egress target resolution.

Tracks when private/sensitive data enters the conversation context (across
turns, within the same turn across sequential tool calls, and monotonically
merged from sub-agents) and gates subsequent non-allowlisted outbound egress.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal

from common.paths import PRIVATE_FILE_MODE, _ensure_private_dir, _ensure_real_descendant
from engine.safety.approval import _EMBEDDED_SECRET_RE
from engine.tool.interface import ToolCall, ToolDefinition

TaintCategory = Literal[
    "private_tool",
    "sensitive_file",
    "outside_workspace",
    "secret_output",
    "host_shell",
    "subagent_inherited",
]

EgressKind = Literal[
    "http_host",
    "search",
    "mcp_server",
    "git_remote",
    "host_shell",
    "unknown",
]

_PRIVATE_KEY_HEADER_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_GIT_REMOTE_URL_RE = re.compile(r"^\s*url\s*=\s*(.+?)\s*$", re.MULTILINE)
_SSH_SCP_HOST_RE = re.compile(r"^(?:[^@\s]+@)?([^:/\s]+):")


@dataclass(frozen=True)
class TaintSource:
    category: TaintCategory
    tool_name: str
    label: str
    run_id: str = ""
    ts: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "category": self.category,
            "tool_name": self.tool_name,
            "label": self.label,
            "run_id": self.run_id,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> TaintSource | None:
        cat = raw.get("category")
        tool = raw.get("tool_name")
        label = raw.get("label")
        if not (isinstance(cat, str) and isinstance(tool, str) and isinstance(label, str)):
            return None
        return cls(
            category=cat,  # type: ignore[arg-type]
            tool_name=tool,
            label=label,
            run_id=str(raw.get("run_id") or ""),
            ts=str(raw.get("ts") or ""),
        )


@dataclass(frozen=True)
class EgressTarget:
    kind: EgressKind
    target: str
    raw_display: str


def _validate_allowlist_entry(raw: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("egress_allowlist entries must be non-empty strings")
    entry = raw.strip().lower().rstrip(".")
    if entry in {"*", "*.*", "**"} or entry.startswith("."):
        raise ValueError(f"Overly broad egress_allowlist entry rejected: {raw!r}")
    if ":" in entry:
        prefix, rest = entry.split(":", 1)
        if prefix not in {"mcp", "git", "search", "host"} or not rest or rest == "*":
            raise ValueError(f"Invalid scoped egress_allowlist entry: {raw!r}")
        if prefix == "git":
            _validate_host_pattern(rest, raw)
        return f"{prefix}:{rest}"
    _validate_host_pattern(entry, raw)
    return entry


def _validate_host_pattern(pattern: str, raw: str) -> None:
    if pattern.startswith("*."):
        suffix = pattern[2:]
        if not suffix or "*" in suffix or "." not in suffix:
            raise ValueError(
                f"Wildcard egress_allowlist host must scope at least two labels: {raw!r}"
            )
        return
    if "*" in pattern:
        raise ValueError(f"Unsupported wildcard in egress_allowlist entry: {raw!r}")
    if "/" in pattern or " " in pattern:
        raise ValueError(f"Invalid host in egress_allowlist entry: {raw!r}")


def host_matches_pattern(host: str, pattern: str) -> bool:
    norm_host = host.strip().lower().rstrip(".")
    norm_pat = pattern.strip().lower().rstrip(".")
    if not norm_host or not norm_pat:
        return False
    if norm_pat.startswith("*."):
        suffix = norm_pat[2:]
        if "." not in suffix:
            return False
        return norm_host.endswith(f".{suffix}") and len(norm_host) > len(suffix) + 1
    return norm_host == norm_pat


class EgressAllowlist:
    """Validated fail-closed allowlist for tainted-session outbound egress."""

    def __init__(self, entries: Iterable[str] = ()) -> None:
        validated: list[str] = []
        for raw in entries:
            norm = _validate_allowlist_entry(raw)
            if norm not in validated:
                validated.append(norm)
        self._entries = tuple(validated)

    @property
    def entries(self) -> tuple[str, ...]:
        return self._entries

    def allowed_http_host_patterns(self) -> tuple[str, ...]:
        return tuple(entry for entry in self._entries if ":" not in entry)

    def allows(self, target: EgressTarget) -> bool:
        if not self._entries:
            return False
        norm_target = target.target.strip().lower().rstrip(".")
        if target.kind == "http_host":
            return any(
                ":" not in entry and host_matches_pattern(norm_target, entry)
                for entry in self._entries
            )
        if target.kind == "git_remote":
            host = norm_target[4:] if norm_target.startswith("git:") else norm_target
            for entry in self._entries:
                if entry.startswith("git:") and host_matches_pattern(host, entry[4:]):
                    return True
            return False
        return norm_target in self._entries


def taint_file_for_session(state_dir: Path, session_id: str) -> Path:
    token = sha256((session_id or "default").encode("utf-8")).hexdigest()
    return state_dir / "taint" / f"{token}.json"


def delete_session_taint(state_dir: Path, session_id: str) -> None:
    """Remove persisted taint state when a session is deleted."""
    taint_dir = state_dir / "taint"
    path = taint_file_for_session(state_dir, session_id)
    if not path.exists() and not path.is_symlink():
        return
    try:
        if taint_dir.is_dir():
            _ensure_real_descendant(state_dir, path.parent)
        path.unlink(missing_ok=True)
    except OSError:
        pass


class SessionTaintStore:
    """Monotonic, session-persistent taint ledger."""

    def __init__(
        self,
        *,
        state_dir: Path | None = None,
        session_id: str | None = None,
    ) -> None:
        self._state_dir = state_dir
        self._session_id = session_id
        self._path: Path | None = (
            taint_file_for_session(state_dir, session_id)
            if state_dir is not None and session_id
            else None
        )
        self._sources: list[TaintSource] = []
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.is_file():
            return
        try:
            if self._state_dir is not None:
                _ensure_real_descendant(self._state_dir, self._path)
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, RuntimeError, json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(raw, dict):
            return
        items = raw.get("sources")
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, dict):
                parsed = TaintSource.from_dict(item)
                if parsed is not None:
                    self._sources.append(parsed)

    def _persist(self) -> None:
        if self._path is None or self._state_dir is None:
            return
        _ensure_private_dir(self._state_dir)
        _ensure_private_dir(self._path.parent)
        _ensure_real_descendant(self._state_dir, self._path)
        payload = {
            "version": 1,
            "session_id": self._session_id,
            "tainted": bool(self._sources),
            "sources": [s.to_dict() for s in self._sources],
        }
        encoded = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(
            prefix=".taint.",
            suffix=".tmp",
            dir=str(self._path.parent),
        )
        tmp_path = Path(tmp_name)
        try:
            os.fchmod(fd, PRIVATE_FILE_MODE)
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._path)
            os.chmod(self._path, PRIVATE_FILE_MODE)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    @property
    def is_tainted(self) -> bool:
        return bool(self._sources)

    @property
    def sources(self) -> tuple[TaintSource, ...]:
        return tuple(self._sources)

    def labels(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for src in self._sources:
            seen.setdefault(src.label, None)
        return tuple(seen)

    def record(
        self,
        category: TaintCategory,
        tool_name: str,
        label: str,
        *,
        run_id: str = "",
    ) -> TaintSource:
        clean_label = label[:120]
        for existing in self._sources:
            if (
                existing.category == category
                and existing.tool_name == tool_name
                and existing.label == clean_label
            ):
                return existing
        source = TaintSource(
            category=category,
            tool_name=tool_name,
            label=clean_label,
            run_id=run_id,
            ts=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        )
        self._sources.append(source)
        self._persist()
        return source

    def merge_from(
        self,
        other_sources: Iterable[TaintSource],
        *,
        as_subagent: bool = False,
    ) -> bool:
        added = False
        for src in other_sources:
            cat: TaintCategory = "subagent_inherited" if as_subagent else src.category
            label = f"subagent:{src.label}" if as_subagent and not src.label.startswith("subagent:") else src.label
            before = len(self._sources)
            self.record(cat, src.tool_name, label, run_id=src.run_id)
            if len(self._sources) > before:
                added = True
        return added

    def clear(self) -> None:
        self._sources.clear()
        if self._path is not None:
            self._path.unlink(missing_ok=True)


def detect_secret_in_text(text: str) -> bool:
    """Return True when tool output contains high-confidence secret material."""
    if not text:
        return False
    if _PRIVATE_KEY_HEADER_RE.search(text):
        return True
    return _EMBEDDED_SECRET_RE.search(text) is not None


def _extract_git_remote_host(repo_dir: Path | None) -> str:
    if repo_dir is None:
        return "unknown"
    git_path = repo_dir / ".git"
    config_file: Path | None = None
    if git_path.is_dir():
        config_file = git_path / "config"
    elif git_path.is_file():
        try:
            text = git_path.read_text(encoding="utf-8", errors="ignore").strip()
            if text.startswith("gitdir:"):
                gdir = Path(text[len("gitdir:"):].strip())
                if not gdir.is_absolute():
                    gdir = repo_dir / gdir
                cand = gdir / "config"
                if cand.is_file():
                    config_file = cand
                elif gdir.parent.name == "worktrees" and (gdir.parent.parent / "config").is_file():
                    config_file = gdir.parent.parent / "config"
        except OSError:
            return "unknown"
    if config_file is None or not config_file.is_file():
        return "unknown"
    try:
        content = config_file.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return "unknown"
    match = _GIT_REMOTE_URL_RE.search(content)
    if not match:
        return "unknown"
    url = match.group(1).strip()
    if "://" in url:
        parsed = urllib.parse.urlparse(url)
        if parsed.hostname:
            return parsed.hostname.lower().rstrip(".")
    scp = _SSH_SCP_HOST_RE.match(url)
    if scp:
        return scp.group(1).lower().rstrip(".")
    return "unknown"


def _mcp_server_name_from_tool(tool_name: str, defn: ToolDefinition | None) -> str:
    if defn is not None and defn.egress_target_arg.startswith("mcp:"):
        return defn.egress_target_arg[4:]
    if tool_name.startswith("mcp_"):
        rest = tool_name[4:]
        return rest.split("_", 1)[0] or "unknown"
    return tool_name


def resolve_egress_target(
    call: ToolCall,
    definition: ToolDefinition | None,
    *,
    working_dir: Path | None = None,
    sandbox_is_degraded: bool = False,
) -> EgressTarget | None:
    """Resolve the standardized egress target for ``call``, or ``None`` if no egress."""
    if definition is None:
        return None

    # 1. Opaque shell command: sandboxed network=False has NO outbound network!
    if definition.opaque_command:
        if sandbox_is_degraded or call.arguments.get("network") is True:
            return EgressTarget(
                kind="host_shell",
                target="host:shell",
                raw_display="shell(network=true)" if not sandbox_is_degraded else "shell(host_degraded)",
            )
        return None

    # 2. git_ops: only action == "push" performs network egress
    if call.name == "git_ops":
        if str(call.arguments.get("action") or "").lower() != "push":
            return None
        cwd_raw = call.arguments.get("cwd")
        repo_path = Path(cwd_raw).expanduser() if isinstance(cwd_raw, str) and cwd_raw else working_dir
        host = _extract_git_remote_host(repo_path)
        return EgressTarget(
            kind="git_remote",
            target=f"git:{host}",
            raw_display=f"git push ({host})",
        )

    is_egress = bool(definition.egress or definition.network_access)
    if not is_egress:
        return None

    # 3. Search tool
    if call.name == "web_search":
        provider = str(call.arguments.get("provider") or "duckduckgo").strip().lower()
        return EgressTarget(
            kind="search",
            target=f"search:{provider}",
            raw_display=f"search:{provider}",
        )

    # 4. MCP tool
    if call.name.startswith("mcp_"):
        server = _mcp_server_name_from_tool(call.name, definition)
        return EgressTarget(
            kind="mcp_server",
            target=f"mcp:{server}",
            raw_display=f"mcp:{server}",
        )

    # 5. URL-based HTTP egress (web_fetch, web_crawl, or custom egress_target_arg="url")
    target_arg = definition.egress_target_arg or ("url" if "url" in call.arguments else "")
    if target_arg == "url":
        raw_url = call.arguments.get("url")
        if isinstance(raw_url, str) and raw_url.strip():
            parsed = urllib.parse.urlparse(raw_url.strip())
            if parsed.hostname:
                host = parsed.hostname.lower().rstrip(".")
                return EgressTarget(
                    kind="http_host",
                    target=host,
                    raw_display=host,
                )

    return EgressTarget(
        kind="unknown",
        target=f"tool:{call.name}",
        raw_display=call.name,
    )


__all__ = (
    "EgressAllowlist",
    "EgressKind",
    "EgressTarget",
    "SessionTaintStore",
    "TaintCategory",
    "TaintSource",
    "delete_session_taint",
    "detect_secret_in_text",
    "host_matches_pattern",
    "resolve_egress_target",
    "taint_file_for_session",
)
