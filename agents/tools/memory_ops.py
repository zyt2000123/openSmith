"""Memory operations tool provider — CRUD for the agent's memory.

Aligned with engine/memory pipeline:
  - add: appends structured candidate evidence to recent.jsonl for policy review
  - search: searches the compiled durable view + recent events
"""
# 记忆写入先记录可审计候选证据，计划和 Todo 始终属于会话状态而非长期记忆。

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TOOL_META = {
    "name": "memory_ops",
    "hidden": True,
    "description": (
        "Memory operations: search memories, record structured evidence candidates, "
        "Plans and Todo items are session state, not memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "add"],
                "description": "The memory operation to perform",
            },
            "query": {
                "type": "string",
                "description": "Search query string (required for search)",
            },
            "content": {
                "type": "string",
                "description": "Candidate memory content (required for add/update)",
            },
            "evidence": {
                "type": "string",
                "description": "Evidence supporting this candidate (required for add)",
            },
            "kind": {
                "type": "string",
                "enum": [
                    "preference", "correction", "decision", "remember", "forget",
                    "verified_fact", "procedure", "pitfall",
                ],
                "description": "Stable memory category required for add; plans and tasks are excluded",
            },
            "scope": {
                "type": "string",
                "enum": ["user", "project"],
                "description": "Ownership scope required for add",
            },
            "evidence_type": {
                "type": "string",
                "enum": ["user_explicit", "tool_result", "test_result", "source_document"],
                "description": "Type of supporting evidence required for add",
            },
        },
        "required": ["action"],
    },
    "is_write_tool": True,
    "permission_level": "write",
    "approval_policy": "policy",
    "read_actions": ["search"],
    "side_effect": "write",
    "concurrency": "serial",
    "execution_environment": "host",
}

_PRIVATE_DIR_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


def _memory_dir(memory_dir: str | Path | None = None) -> Path:
    if memory_dir is not None:
        return Path(memory_dir).expanduser()
    return Path.home() / ".agent-smith" / "agent" / "memory"


def _ensure_private_dir(path: Path) -> None:
    """Create ``path`` and every missing ancestor with 0700.

    ``agents/`` cannot import ``common.paths``, so the mode is restated here.
    Two traps this avoids: ``mkdir(parents=True, mode=...)`` applies the mode
    only to the final component and lets the ancestors it creates fall back to
    the process umask, and ``exist_ok=True`` leaves an existing directory's mode
    untouched — hence walking up, then creating and chmod-ing each level.
    """
    missing: list[Path] = []
    probe = path
    while not probe.exists() and probe.parent != probe:
        missing.append(probe)
        probe = probe.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True, mode=_PRIVATE_DIR_MODE)
        directory.chmod(_PRIVATE_DIR_MODE)
    path.chmod(_PRIVATE_DIR_MODE)


def _check_sensitive(text: str, memory_api: Any) -> str | None:
    if memory_api.contains_secret(text):
        return "Memory rejected: contains sensitive information"
    if memory_api.contains_injection(text):
        return "Memory rejected: contains instruction-injection patterns"
    return None


def _sanitize_for_tool_output(text: str, memory_api: Any) -> str:
    """Keep legacy memory content safe before it re-enters model context."""
    return memory_api.sanitize_memory_text(text)[0]


def _safe_file_in_dir(root: Path, path: Path, memory_api: Any) -> Path | None:
    return memory_api.safe_file_in_dir(root, path)


def _safe_markdown_files(directory: Path, memory_api: Any) -> list[Path]:
    return memory_api.safe_markdown_files(directory)


def _sanitize_event_value_for_storage(value: str, memory_api: Any) -> str:
    return memory_api.sanitize_event_value(value)


async def execute(
    *,
    action: str,
    query: str | None = None,
    content: str | None = None,
    evidence: str | None = None,
    kind: str | None = None,
    scope: str | None = None,
    evidence_type: str | None = None,
    memory_dir: str | Path | None = None,
    memory_api: Any | None = None,
    **_: object,
) -> str:
    if memory_api is None:
        return "Error: memory runtime capability was not provided"
    mem_dir = _memory_dir(memory_dir)
    await asyncio.to_thread(_ensure_private_dir, mem_dir)

    if action == "search":
        if not query:
            return "Error: 'query' is required for search action"
        return await _search(mem_dir, query, memory_api)

    elif action == "add":
        if not content:
            return "Error: 'content' is required for add action"
        if not evidence:
            return "Error: 'evidence' is required for add action"
        if not kind:
            return "Error: 'kind' is required for add action"
        if not scope:
            return "Error: 'scope' is required for add action"
        if not evidence_type:
            return "Error: 'evidence_type' is required for add action"
        if kind in {"plan", "task", "todo", "task_step"}:
            return "Error: plans and tasks belong in Todo/session state, not persistent memory"
        if kind not in memory_api.MANUAL_MEMORY_KINDS:
            return "Error: unsupported memory kind; record only stable evidence categories"
        if scope not in {"user", "project"}:
            return "Error: scope must be 'user' or 'project'"
        if evidence_type not in memory_api.MANUAL_EVIDENCE_TYPES:
            return "Error: unsupported evidence_type"
        rejection = _check_sensitive(content, memory_api) or _check_sensitive(evidence, memory_api)
        if rejection:
            return rejection
        return await asyncio.to_thread(
            _append_event,
            mem_dir,
            content,
            evidence,
            kind,
            scope,
            evidence_type,
            memory_api,
        )

    return f"Error: unknown action '{action}'. Use: search, add"


def _append_event(
    mem_dir: Path,
    content: str,
    evidence: str,
    kind: str,
    scope: str,
    evidence_type: str,
    memory_api: Any,
) -> str:
    """Append structured candidate evidence for policy-governed compilation."""
    recent_file = mem_dir / "recent.jsonl"
    now = datetime.now(timezone.utc).isoformat()
    entry = {
        "task": _sanitize_event_value_for_storage(f"[memory] {content}", memory_api),
        "summary": _sanitize_event_value_for_storage(f"Evidence: {evidence}", memory_api),
        "timestamp": now,
        "kind": kind,
        "scope": scope,
        "evidence": evidence_type,
        "evidence_type": evidence_type,
    }
    with open(recent_file, "a", encoding="utf-8") as f:
        # Memory holds user evidence, so it carries the runtime's 0600 rule.
        # fchmod acts on the open descriptor: no path race, and it also repairs
        # a file an earlier build created with the process umask.
        os.fchmod(f.fileno(), _PRIVATE_FILE_MODE)
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return "OK: candidate evidence recorded for policy review; it is not durable memory"


def _search_sync(mem_dir: Path, query: str, memory_api: Any) -> str:
    safe_query = _sanitize_for_tool_output(query, memory_api)
    keywords = safe_query.lower().split()
    if not keywords:
        return "No keywords provided"

    matches: list[str] = []

    for name in memory_api.MEMORY_LAYER_FILES:
        path = _safe_file_in_dir(mem_dir, mem_dir / name, memory_api)
        if path is not None:
            content = _sanitize_for_tool_output(path.read_text(encoding="utf-8"), memory_api)
            if any(kw in content.lower() for kw in keywords):
                matches.append(f"- [{name}] {content[:200]}")

    recent_file = mem_dir / "recent.jsonl"
    if recent_file.is_file():
        try:
            for line in recent_file.read_text(encoding="utf-8").strip().splitlines()[-20:]:
                if any(kw in line.lower() for kw in keywords):
                    try:
                        entry = json.loads(line)
                        if not isinstance(entry, dict):
                            continue
                        task = _sanitize_for_tool_output(str(entry.get("task", "?")), memory_api)
                        summary = _sanitize_for_tool_output(str(entry.get("summary", "?")), memory_api)
                        content = f"{task} {summary}"
                        if not any(kw in content.lower() for kw in keywords):
                            continue
                        matches.append(
                            f"- [recent] {task} -> {summary[:80]}"
                        )
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass

    if not matches:
        return f"No matches for '{safe_query}'"
    return f"Found {len(matches)} match(es):\n" + "\n".join(matches)


async def _search(mem_dir: Path, query: str, memory_api: Any) -> str:
    return await asyncio.to_thread(_search_sync, mem_dir, query, memory_api)
