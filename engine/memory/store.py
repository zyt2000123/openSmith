"""Memory store — recent.jsonl as sole event source + episode FTS5 search.

Provides:
  - search_relevant_memories(): FTS5 episode search for prompt injection
  - save_conversation_memory(): append events + trigger compilation/dream
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from ._files import (
    atomic_write_text,
    interprocess_file_lock,
    safe_file_in_dir,
    safe_markdown_files,
    sanitize_memory_text,
)

logger = logging.getLogger(__name__)

MemoryMaintenance = Callable[[Path], Awaitable[bool]]


@dataclass(frozen=True)
class RelevantMemory:
    """Query-time memory results that retain their source boundary."""

    durable: str = ""
    episodes: str = ""

    def render(self) -> str:
        return "\n\n".join(part for part in (self.durable, self.episodes) if part)


# ---------------------------------------------------------------------------
# Query-time retrieval: search episodes via FTS5
# ---------------------------------------------------------------------------

_MAX_EPISODE_CONTEXT_CHARS = 6000
_MAX_DURABLE_CONTEXT_CHARS = 4000


async def search_relevant_memories(agent_dir: Path, query: str, top_k: int = 3) -> str:
    """Backward-compatible flattened query-time memory retrieval."""
    return (await retrieve_relevant_memory(agent_dir, query, top_k)).render()


async def retrieve_relevant_memory(
    agent_dir: Path,
    query: str,
    top_k: int = 3,
) -> RelevantMemory:
    """Search durable and episode memory while retaining their source boundary.

    Recent working memory remains a bounded passive layer. Durable and episode
    memory are recalled on demand. Every failure degrades to whatever safe
    section was already found, never to a blocked prompt assembly.
    """
    if not query.strip():
        return RelevantMemory()

    try:
        durable = _select_relevant_durable(agent_dir / "memory", query)
    except Exception:
        logger.warning("durable-memory retrieval failed", exc_info=True)
        durable = ""

    episodes_dir = agent_dir / "memory" / "episodes"
    if not episodes_dir.is_dir():
        return RelevantMemory(durable=durable)

    try:
        from .search import SearchIndex

        idx = SearchIndex(episodes_dir)
        await idx.open()
        try:
            await _sync_episode_index(idx, episodes_dir)

            hits = await idx.search(query, top_k)
            if not hits:
                return RelevantMemory(durable=durable)

            lines = ["## Relevant Episodes"]
            total_chars = 0
            for hit in hits:
                ep_path = safe_file_in_dir(episodes_dir, episodes_dir / f"{hit['id']}.md")
                if ep_path is None:
                    continue
                content, _, _ = sanitize_memory_text(ep_path.read_text(encoding="utf-8"))
                content = content.strip()
                if not content:
                    continue
                if total_chars + len(content) > _MAX_EPISODE_CONTEXT_CHARS:
                    continue
                lines.append(content)
                total_chars += len(content)

            if len(lines) > 1:
                return RelevantMemory(durable=durable, episodes="\n\n".join(lines))
            return RelevantMemory(durable=durable)
        finally:
            await idx.close()
    except Exception:
        logger.warning("episode-memory retrieval failed", exc_info=True)
        return RelevantMemory(durable=durable)


def _select_relevant_durable(memory_dir: Path, query: str) -> str:
    """Return matching durable bullets using dependency-free lexical recall."""
    durable_path = safe_file_in_dir(memory_dir, memory_dir / "durable.md")
    if durable_path is None:
        return ""
    content, _, _ = sanitize_memory_text(durable_path.read_text(encoding="utf-8"))
    terms = _query_terms(query)
    if not terms:
        return ""

    matches: list[tuple[int, int, str]] = []
    for index, line in enumerate(content.splitlines()):
        if not line.lstrip().startswith("-"):
            continue
        lowered = line.lower()
        score = sum(1 for term in terms if term in lowered)
        if score:
            matches.append((-score, index, line))
    if not matches:
        return ""

    selected: list[str] = []
    used = 0
    for _, _, line in sorted(matches):
        if used + len(line) > _MAX_DURABLE_CONTEXT_CHARS:
            continue
        selected.append(line)
        used += len(line)
    if not selected:
        return ""
    return "## Relevant Durable Memory\n\n" + "\n".join(selected)


def _query_terms(query: str) -> set[str]:
    lowered = query.lower()
    terms = {
        token
        for token in re.findall(r"[a-z0-9_./-]{2,}", lowered)
        if token not in {"the", "and", "for", "with", "this", "that"}
    }
    for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", lowered):
        terms.update(sequence[index:index + 2] for index in range(len(sequence) - 1))
    return terms


_EPISODE_INDEX_STATE = ".index_state.json"


def _load_episode_index_state(path: Path) -> dict[str, str]:
    """Read the disposable per-file index state, rebuilding on malformed data."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict) or not all(
        isinstance(entry_id, str) and isinstance(signature, str)
        for entry_id, signature in raw.items()
    ):
        return {}
    return raw


async def _sync_episode_index(idx, episodes_dir: Path) -> None:
    """Synchronize the FTS index from current episode files.

    State is keyed per episode rather than by a global timestamp, so copied or
    restored files with an older mtime still enter the index. The state is
    disposable and is only committed after index writes and stale-row removal
    have succeeded.
    """
    state_path = episodes_dir / _EPISODE_INDEX_STATE
    previous_state = _load_episode_index_state(state_path)
    current_state: dict[str, str] = {}

    for resolved in safe_markdown_files(episodes_dir):
        stat = resolved.stat()
        entry_id = resolved.stem
        signature = f"{stat.st_mtime_ns}:{stat.st_size}"
        current_state[entry_id] = signature
        if previous_state.get(entry_id) != signature:
            content, _, _ = sanitize_memory_text(resolved.read_text(encoding="utf-8"))
            await idx.index_entry(entry_id, content, "episode")

    await idx.remove_missing_entries(set(current_state), "episode")

    if current_state != previous_state:
        atomic_write_text(
            state_path,
            json.dumps(current_state, ensure_ascii=False, sort_keys=True),
        )
    (episodes_dir / ".index_mtime").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Conversation-level memory persistence
# ---------------------------------------------------------------------------

_COMPILE_INTERVAL = 5
_MAX_EVENT_VALUE_CHARS = 16_000
_MAX_LEARNING_SIGNALS = 16

_LEARNING_SIGNAL_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("forget", "user", re.compile(r"忘记|不要再记|forget\b", re.IGNORECASE)),
    (
        "correction",
        "user",
        re.compile(r"不对|纠正|不是.+而是|that's wrong|actually\b", re.IGNORECASE),
    ),
    (
        "preference",
        "user",
        re.compile(
            r"我希望|我喜欢|我习惯|默认.{0,12}(?:用|使用|回答)|以后.{0,12}(?:请|用|不要)|"
            r"\bi prefer\b|\bplease always\b|\bi want you to\b",
            re.IGNORECASE,
        ),
    ),
    ("decision", "project", re.compile(r"决定|定下来|就按|we decided", re.IGNORECASE)),
    ("remember", "project", re.compile(r"记住|记一下|remember\b", re.IGNORECASE)),
)


def _bounded_event_value(value: str) -> str:
    """Keep normal conversation events intact and mark exceptional truncation."""
    if len(value) <= _MAX_EVENT_VALUE_CHARS:
        return value

    marker = "\n\n[Memory event truncated for storage]\n\n"
    available = _MAX_EVENT_VALUE_CHARS - len(marker)
    if available <= 0:
        return value[:_MAX_EVENT_VALUE_CHARS]
    head = available // 2
    tail = available - head
    return f"{value[:head]}{marker}{value[-tail:]}"


def _increment_counter(counter_file: Path, retry_threshold: int) -> int:
    with interprocess_file_lock(counter_file):
        count = 0
        if counter_file.is_file():
            try:
                count = int(counter_file.read_text().strip())
            except (ValueError, OSError):
                count = 0
        count = min(count + 1, retry_threshold)
        atomic_write_text(counter_file, str(count))
        return count


def _reset_counter(counter_file: Path) -> None:
    with interprocess_file_lock(counter_file):
        atomic_write_text(counter_file, "0")


async def save_conversation_memory(
    agent_dir: Path,
    user_msg: str,
    reply: str,
    had_tools: bool,
    *,
    learning_signals: list[str] | None = None,
    turn_status: str = "completed",
    turn_reason: str | None = None,
    compile_maintenance: MemoryMaintenance | None = None,
    dream_maintenance: MemoryMaintenance | None = None,
) -> None:
    """Append useful work/learning evidence and schedule memory maintenance.

    Incomplete turns are recorded as ``partial_work`` so the next run can see
    useful tool-backed progress without treating an unfinished reply as a
    completed project fact.
    """
    explicit_signal = _detect_learning_signal(user_msg)
    stable_signals = [
        sanitize_event_value(signal)
        for signal in (learning_signals or [])[:_MAX_LEARNING_SIGNALS]
        if signal.strip()
    ]
    if not had_tools and explicit_signal is None and not stable_signals:
        return

    memory_dir = agent_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    recent_file = memory_dir / "recent.jsonl"
    now = datetime.now(timezone.utc).isoformat()
    bounded_task = sanitize_event_value(user_msg)
    bounded_summary = sanitize_event_value(reply)

    entries: list[dict] = []
    if had_tools:
        work_entry = {
            "task": bounded_task,
            "summary": bounded_summary,
            "timestamp": now,
            "kind": "work" if turn_status == "completed" else "partial_work",
            "scope": "project",
            "evidence": "tool_result" if turn_status == "completed" else "partial_tool_result",
        }
        if turn_status != "completed":
            work_entry["status"] = sanitize_event_value(turn_status)
            if turn_reason:
                work_entry["reason"] = sanitize_event_value(turn_reason)
        entries.append(work_entry)
    if explicit_signal is not None or stable_signals:
        kind, scope = explicit_signal or ("pattern", "user")
        signal_entry = {
            "task": bounded_task,
            "summary": bounded_summary,
            "timestamp": now,
            "kind": kind,
            "scope": scope,
            "evidence": "user_explicit" if explicit_signal is not None else "repeated_observation",
        }
        if stable_signals:
            signal_entry["signals"] = stable_signals
        entries.append(signal_entry)

    with open(recent_file, "a", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # Periodic compilation (recent + durable)
    counter_file = memory_dir / ".compile_counter"
    count = _increment_counter(counter_file, _COMPILE_INTERVAL)

    has_learning_signal = explicit_signal is not None or bool(stable_signals)
    if (count >= _COMPILE_INTERVAL or has_learning_signal) and compile_maintenance is not None:
        if await compile_maintenance(memory_dir):
            _reset_counter(counter_file)

    # Low-frequency Dream consolidation (separate counter)
    from .dream import DREAM_INTERVAL
    dream_counter = memory_dir / ".dream_counter"
    d_count = _increment_counter(dream_counter, DREAM_INTERVAL)

    if d_count >= DREAM_INTERVAL and dream_maintenance is not None:
        if await dream_maintenance(memory_dir):
            _reset_counter(dream_counter)


def sanitize_event_value(value: str) -> str:
    """Bound an event and redact values unsafe for future prompt use."""
    bounded = _bounded_event_value(value)
    cleaned, secrets_removed, injections_removed = sanitize_memory_text(bounded)
    if cleaned.strip():
        return cleaned
    if secrets_removed:
        return "[REDACTED — contained sensitive information]"
    if injections_removed:
        return "[REDACTED — contained instruction-injection patterns]"
    return bounded


def _detect_learning_signal(user_message: str) -> tuple[str, str] | None:
    for kind, scope, pattern in _LEARNING_SIGNAL_PATTERNS:
        if pattern.search(user_message):
            return kind, scope
    return None
