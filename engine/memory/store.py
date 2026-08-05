"""Memory store — recent.jsonl as sole event source + episode FTS5 search.

Provides:
  - search_relevant_memories(): FTS5 episode search for prompt injection
  - save_conversation_memory(): append events + trigger compilation/nudge/dream
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from ._files import (
    append_private_lines,
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
    *,
    embedding_provider: object | None = None,
) -> RelevantMemory:
    """Search durable and episode memory while retaining their source boundary.

    Recent working memory remains a bounded passive layer. Durable and episode
    memory are recalled on demand. Every failure degrades to whatever safe
    section was already found, never to a blocked prompt assembly.
    """
    if not query.strip():
        return RelevantMemory()

    try:
        # The durable scan is pure blocking file I/O; keep it off the event loop.
        durable = await asyncio.to_thread(
            _select_relevant_durable, agent_dir / "memory", query
        )
    except Exception:
        logger.warning("durable-memory retrieval failed", exc_info=True)
        durable = ""

    episodes_dir = agent_dir / "memory" / "episodes"
    if not episodes_dir.is_dir():
        return RelevantMemory(durable=durable)

    durable_bullets = [
        line for line in durable.splitlines() if line.lstrip().startswith("-")
    ]
    # Durable memory routes the *generated* topic pages: a topic is eligible only
    # when a durable bullet selected for this question covers it.  A
    # ``generated_ids`` of ``None`` means no snapshot exists yet, so nothing is
    # scoped and every episode keeps its original recall.
    routed_topics: tuple[str, ...] = ()
    topic_entry_ids: tuple[str, ...] = ()
    generated_ids: set[str] | None = None
    snapshot_topics: frozenset[str] | None = None
    try:
        from .knowledge import TopicAssociationStore

        associations = TopicAssociationStore(agent_dir / "memory")
        routed_topics = associations.topics_for_entries(durable_bullets)
        if associations.has_state():
            topic_entry_ids = associations.file_ids_for_topics(routed_topics)
            topics_map, files_map = associations.snapshot()
            generated_ids = set(files_map.values())
            snapshot_topics = frozenset(topics_map)
    except Exception:
        logger.warning("durable topic routing failed", exc_info=True)
        routed_topics = ()
        topic_entry_ids = ()
        generated_ids = None
        snapshot_topics = None

    try:
        from .search import SearchIndex

        idx = SearchIndex(episodes_dir)
        await idx.open()
        try:
            indexed_ids = await _sync_episode_index(idx, episodes_dir)
            # A generated topic page answers only to durable routing, while an
            # episode written before the snapshot existed keeps its original
            # unscoped recall — adopting the knowledge layer must not strand the
            # memory a profile already accumulated.
            routed_entry_ids = (
                None
                if generated_ids is None
                else topic_entry_ids + tuple(sorted(indexed_ids - generated_ids))
            )
            semantic_hits: list[dict[str, object]] = []

            # Vector search covers generated pages only, so it is keyed on the
            # routed topics and must never see the legacy ids added above.
            if embedding_provider is not None and routed_topics:
                try:
                    from .vector import TopicVectorIndex

                    vector_index = TopicVectorIndex(episodes_dir)
                    await vector_index.sync(
                        _topic_documents(episodes_dir, routed_topics, topic_entry_ids),
                        embedding_provider,
                        valid_topics=snapshot_topics,
                    )
                    semantic_hits = await vector_index.search(
                        query, routed_topics, embedding_provider, top_k
                    )
                except Exception:
                    logger.warning("topic vector retrieval failed; falling back to FTS", exc_info=True)

            hits = await idx.search(
                query,
                top_k,
                entry_ids=routed_entry_ids,
            )
            lines = await asyncio.to_thread(
                _read_episode_hits,
                episodes_dir,
                hits,
                _MAX_EPISODE_CONTEXT_CHARS,
                durable_bullets,
            )
            if semantic_hits:
                lines = _merge_knowledge_fragments(
                    [
                        _without_durable_repetition(str(hit["text"]), durable_bullets)
                        for hit in semantic_hits
                    ],
                    lines,
                )
            if lines:
                episode_text = "\n\n".join(lines)
                return RelevantMemory(durable=durable, episodes=episode_text)
            return RelevantMemory(durable=durable)
        finally:
            await idx.close()
    except Exception:
        logger.warning("episode-memory retrieval failed", exc_info=True)
        return RelevantMemory(durable=durable)


def _read_episode_hits(
    episodes_dir: Path,
    hits: list[dict],
    max_chars: int,
    durable_entries: list[str] | None = None,
) -> list[str]:
    """Read and sanitize matched episode files without blocking the event loop."""
    durable_entries = durable_entries or []
    lines: list[str] = ["## Relevant Episodes"]
    total_chars = 0
    for hit in hits:
        ep_path = safe_file_in_dir(episodes_dir, episodes_dir / f"{hit['id']}.md")
        if ep_path is None:
            continue
        content, _, _ = sanitize_memory_text(ep_path.read_text(encoding="utf-8"))
        content = _without_durable_repetition(content, durable_entries)
        content = content.strip()
        if not content:
            continue
        if total_chars + len(content) > max_chars:
            continue
        lines.append(content)
        total_chars += len(content)
    return lines if len(lines) > 1 else []


def _without_durable_repetition(content: str, durable_entries: list[str]) -> str:
    """Drop episode lines that only restate a durable bullet already in context.

    Substring removal cut a durable phrase out of the middle of an episode
    sentence — "我们决定<X>，而不是 Y" lost its subject and inverted the meaning —
    and a durable ``---`` separator erased every ``---`` in the page.  Compare
    whole lines on collapsed whitespace instead.
    """
    duplicates = {" ".join(entry.split()) for entry in durable_entries if entry.strip()}
    if not duplicates:
        return content
    return "\n".join(
        line
        for line in content.splitlines()
        if " ".join(line.split()) not in duplicates
    )


def _merge_knowledge_fragments(semantic: list[str], lexical: list[str]) -> list[str]:
    """Keep semantic and lexical evidence while dropping exact duplicate fragments."""
    result = ["## Relevant Topic Knowledge"]
    seen: set[str] = set()
    for fragment in [*semantic, *lexical[1:]]:
        normalized = " ".join(fragment.split())
        if normalized and normalized not in seen:
            result.append(fragment)
            seen.add(normalized)
    return result if len(result) > 1 else []


def _topic_documents(episodes_dir: Path, topics: tuple[str, ...], file_ids: tuple[str, ...]) -> dict[str, str]:
    documents: dict[str, str] = {}
    for topic, file_id in zip(topics, file_ids, strict=False):
        path = safe_file_in_dir(episodes_dir, episodes_dir / f"{file_id}.md")
        if path is None:
            continue
        content, _, _ = sanitize_memory_text(path.read_text(encoding="utf-8"))
        if content.strip():
            documents[topic] = content
    return documents


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


def _scan_episode_changes(
    episodes_dir: Path,
    previous_state: dict[str, str],
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Scan episode files for signature changes; runs on a worker thread."""
    current_state: dict[str, str] = {}
    changed: list[tuple[str, str]] = []
    for resolved in safe_markdown_files(episodes_dir):
        stat = resolved.stat()
        entry_id = resolved.stem
        signature = f"{stat.st_mtime_ns}:{stat.st_size}"
        current_state[entry_id] = signature
        if previous_state.get(entry_id) != signature:
            content, _, _ = sanitize_memory_text(resolved.read_text(encoding="utf-8"))
            changed.append((entry_id, content))
    return current_state, changed


async def _sync_episode_index(idx, episodes_dir: Path) -> set[str]:
    """Synchronize the FTS index from current episode files, returning their ids.

    The returned set is the episode ids currently backed by a file, which lets a
    caller separate generated topic pages from pre-existing episodes without a
    second directory scan on the per-query hot path.

    State is keyed per episode rather than by a global timestamp, so copied or
    restored files with an older mtime still enter the index. The state is
    disposable and is only committed after index writes and stale-row removal
    have succeeded.  File scanning runs on a worker thread (this is on the
    per-query hot path) and index writes are batched into one transaction.
    """
    state_path = episodes_dir / _EPISODE_INDEX_STATE
    previous_state = await asyncio.to_thread(_load_episode_index_state, state_path)
    current_state, changed = await asyncio.to_thread(
        _scan_episode_changes, episodes_dir, previous_state
    )

    if changed:
        await idx.index_entries(
            [(entry_id, content, "episode") for entry_id, content in changed]
        )

    await idx.remove_missing_entries(set(current_state), "episode")

    if current_state != previous_state:
        atomic_write_text(
            state_path,
            json.dumps(current_state, ensure_ascii=False, sort_keys=True),
        )
    (episodes_dir / ".index_mtime").unlink(missing_ok=True)
    return set(current_state)


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


# ---------------------------------------------------------------------------
# Maintenance retry backoff
# ---------------------------------------------------------------------------

# A due-but-failing maintenance lane (transport/provider errors) stays due while
# it fails because counters are clamped at their threshold.  Persist a
# last-attempt timestamp per lane so inline turns and deferred background
# scheduling skip retries inside this cooldown instead of hammering the LLM.
_RETRY_COOLDOWN_SECONDS = 600.0


def _retry_marker_path(memory_dir: Path, kind: str) -> Path:
    return memory_dir / f".{kind}_retry_attempt"


def _record_retry_attempt(memory_dir: Path, kind: str) -> None:
    try:
        atomic_write_text(_retry_marker_path(memory_dir, kind), str(time.time()))
    except OSError:
        logger.warning("could not record memory maintenance retry attempt", exc_info=True)


def _clear_retry_attempt(memory_dir: Path, kind: str) -> None:
    _retry_marker_path(memory_dir, kind).unlink(missing_ok=True)


def _in_retry_cooldown(memory_dir: Path, kind: str) -> bool:
    marker = _retry_marker_path(memory_dir, kind)
    try:
        attempted_at = float(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return time.time() - attempted_at < _RETRY_COOLDOWN_SECONDS


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
    nudge_maintenance: MemoryMaintenance | None = None,
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

    append_private_lines(
        recent_file,
        [json.dumps(entry, ensure_ascii=False) for entry in entries],
    )

    # Periodic compilation (recent + durable)
    counter_file = memory_dir / ".compile_counter"
    count = _increment_counter(counter_file, _COMPILE_INTERVAL)

    has_learning_signal = explicit_signal is not None or bool(stable_signals)
    # An explicit learning signal is a fresh trigger and always deserves a
    # compile.  The counter-driven lane skips retries while a transport/provider
    # failure is inside its cooldown so a due-but-failing lane cannot call the
    # LLM every turn.
    if has_learning_signal and compile_maintenance is not None:
        if await compile_maintenance(memory_dir):
            _reset_counter(counter_file)
    elif (
        count >= _COMPILE_INTERVAL
        and compile_maintenance is not None
        and not _in_retry_cooldown(memory_dir, "compile")
    ):
        if await compile_maintenance(memory_dir):
            _reset_counter(counter_file)

    # Periodic quality review.  This is deliberately separate from compilation:
    # it can append only structured candidate evidence, never durable memory.
    # Its maintenance callback immediately reuses the normal compiler when a
    # candidate was added, so a twenty-event nudge does not wait for another
    # five-event compilation cadence to become visible.
    from .nudge import NUDGE_INTERVAL

    nudge_counter = memory_dir / ".nudge_counter"
    nudge_count = _increment_counter(nudge_counter, NUDGE_INTERVAL)
    if (
        nudge_count >= NUDGE_INTERVAL
        and nudge_maintenance is not None
        and not _in_retry_cooldown(memory_dir, "nudge")
    ):
        if await nudge_maintenance(memory_dir):
            _reset_counter(nudge_counter)

    # Low-frequency Dream consolidation (separate counter)
    from .dream import DREAM_INTERVAL
    dream_counter = memory_dir / ".dream_counter"
    d_count = _increment_counter(dream_counter, DREAM_INTERVAL)

    if (
        d_count >= DREAM_INTERVAL
        and dream_maintenance is not None
        and not _in_retry_cooldown(memory_dir, "dream")
    ):
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
