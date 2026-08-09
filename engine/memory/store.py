"""Memory store — recent.jsonl is the sole event source.

Provides:
  - save_conversation_memory(): append events + trigger compilation/dream

There is no query-time retrieval here: both rendered views are budget-capped
and injected whole, so nothing needs to be searched or ranked.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from ._files import (
    append_private_lines,
    atomic_write_text,
    interprocess_file_lock,
    sanitize_memory_text,
)

logger = logging.getLogger(__name__)

MemoryMaintenance = Callable[[Path], Awaitable[bool]]


# ---------------------------------------------------------------------------
# Conversation-level memory persistence
# ---------------------------------------------------------------------------

_COMPILE_INTERVAL = 10
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
