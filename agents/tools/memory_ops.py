"""Memory operations tool provider — CRUD for the agent's memory.

Aligned with engine/memory pipeline:
  - add: appends structured candidate evidence to recent.jsonl for policy review
  - search: searches across compiled layers + episodes + recent events
  - episode: creates an episode archive (wiki mode)
  - update/remove: operate on episodes only
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EpisodeRunner = Callable[[Path, str, list[dict]], Awaitable[Path | None]]

TOOL_META = {
    "name": "memory_ops",
    "hidden": True,
    "description": (
        "Memory operations: search memories, record structured evidence candidates, "
        "and manage episodes. Plans and Todo items are session state, not memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "add", "update", "remove", "episode"],
                "description": "The memory operation to perform",
            },
            "topic": {
                "type": "string",
                "description": "Episode topic name (required for episode action)",
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
            "episode_id": {
                "type": "string",
                "description": "Episode file stem (required for update/remove)",
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

def _memory_dir(memory_dir: str | Path | None = None) -> Path:
    if memory_dir is not None:
        return Path(memory_dir).expanduser()
    return Path.home() / ".agent-smith" / "agent" / "memory"


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
    topic: str | None = None,
    episode_id: str | None = None,
    memory_dir: str | Path | None = None,
    episode_runner: EpisodeRunner | None = None,
    memory_api: Any | None = None,
    **_: object,
) -> str:
    if memory_api is None:
        return "Error: memory runtime capability was not provided"
    mem_dir = _memory_dir(memory_dir)
    await asyncio.to_thread(mem_dir.mkdir, parents=True, exist_ok=True)

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

    elif action == "episode":
        if not topic:
            return "Error: 'topic' is required for episode action"
        rejection = _check_sensitive(topic, memory_api)
        if rejection:
            return rejection
        return await _create_episode(mem_dir, topic, episode_runner=episode_runner)

    elif action == "update":
        if not episode_id:
            return "Error: 'episode_id' is required for update action"
        if not content:
            return "Error: 'content' is required for update action"
        rejection = _check_sensitive(content, memory_api)
        if rejection:
            return rejection
        return await asyncio.to_thread(
            _update_episode,
            mem_dir,
            episode_id,
            content,
            memory_api,
        )

    elif action == "remove":
        if not episode_id:
            return "Error: 'episode_id' is required for remove action"
        return await _remove_episode(mem_dir, episode_id, memory_api)

    return f"Error: unknown action '{action}'. Use: search, add, episode, update, remove"


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

    episodes_dir = mem_dir / "episodes"
    for ep in _safe_markdown_files(episodes_dir, memory_api):
        content = _sanitize_for_tool_output(ep.read_text(encoding="utf-8"), memory_api)
        if any(kw in content.lower() for kw in keywords):
            matches.append(f"- [episode:{ep.stem}] {content[:120]}")

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


async def _create_episode(
    mem_dir: Path,
    topic: str,
    *,
    episode_runner: EpisodeRunner | None = None,
) -> str:
    recent_file = mem_dir / "recent.jsonl"
    def load_related() -> list[dict] | None:
        if not recent_file.is_file():
            return None
        topic_keywords = topic.lower().split()
        related_events: list[dict] = []
        for line in recent_file.read_text(encoding="utf-8").strip().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            text = f"{entry.get('task', '')} {entry.get('summary', '')}".lower()
            if any(kw in text for kw in topic_keywords):
                related_events.append(entry)
        return related_events

    related = await asyncio.to_thread(load_related)
    if related is None:
        return "Error: no recent events to summarize"

    if not related:
        return f"No events found matching topic '{topic}'"

    if episode_runner is None:
        return "Error: episode generation unavailable — no episode runner configured"

    try:
        path = await episode_runner(mem_dir, topic, related)
        if path:
            return f"OK: episode saved to {path.name} ({len(related)} related events)"
        return "Error: episode generation returned no output"
    except Exception as e:
        return f"Error generating episode: {type(e).__name__}: {e}"


def _update_episode(mem_dir: Path, episode_id: str, content: str, memory_api: Any) -> str:
    if Path(episode_id).name != episode_id:
        return "Error: invalid episode_id"
    ep_path = mem_dir / "episodes" / f"{episode_id}.md"
    if not ep_path.is_file():
        return f"Error: episode '{episode_id}' not found"
    ep_root = (mem_dir / "episodes").resolve()
    if not ep_path.resolve().is_relative_to(ep_root):
        return "Error: invalid episode path"

    memory_api.atomic_write_text(ep_path, content)
    return f"OK: updated episode '{episode_id}'"


async def _remove_episode(mem_dir: Path, episode_id: str, memory_api: Any) -> str:
    def remove_file() -> str | None:
        if Path(episode_id).name != episode_id:
            return "Error: invalid episode_id"
        ep_path = mem_dir / "episodes" / f"{episode_id}.md"
        if not ep_path.is_file():
            return f"Error: episode '{episode_id}' not found"
        ep_root = (mem_dir / "episodes").resolve()
        if not ep_path.resolve().is_relative_to(ep_root):
            return "Error: invalid episode path"
        ep_path.unlink()
        return None

    error = await asyncio.to_thread(remove_file)
    if error is not None:
        return error
    try:
        await memory_api.remove_episode_from_index(mem_dir, episode_id)
    except Exception:
        return f"OK: removed episode '{episode_id}' (search index update failed — will self-heal)"
    return f"OK: removed episode '{episode_id}'"
