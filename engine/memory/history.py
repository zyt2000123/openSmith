"""Append-only audit history for automatic memory changes."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ._files import append_private_lines, atomic_write_text, sanitize_memory_text

logger = logging.getLogger(__name__)

# Retention bounds applied during Dream maintenance so the audit log cannot grow
# without limit.
_MAX_HISTORY_ENTRIES = 500
_MAX_HISTORY_AGE_DAYS = 90


def append_memory_history(
    memory_dir: Path,
    *,
    target: str,
    policy_version: int,
    status: str,
    old_text: str = "",
    new_text: str = "",
    review_rounds: int = 0,
    error: str | None = None,
    notes: list[str] | None = None,
) -> bool:
    """Append one sanitized compile/review/write outcome without blocking memory."""
    cleaned_error: str | None = None
    if error:
        cleaned_error, _, _ = sanitize_memory_text(error)
        cleaned_error = cleaned_error.strip()[:500] or "redacted error"

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "target": target,
        "policy_version": policy_version,
        "status": status,
        "old_hash": _digest(old_text),
        "new_hash": _digest(new_text),
        "review_rounds": max(0, review_rounds),
        "error": cleaned_error,
    }
    if notes:
        # What the change set proposed but did not land: rejected edits and
        # bullets evicted for budget.  Sanitized like everything else, because a
        # rejected edit can still quote the content that got it rejected.
        cleaned_notes = []
        for note in notes[:20]:
            cleaned, _, _ = sanitize_memory_text(str(note))
            cleaned = cleaned.strip()[:300]
            if cleaned:
                cleaned_notes.append(cleaned)
        if cleaned_notes:
            entry["not_written"] = cleaned_notes
    try:
        memory_dir.mkdir(parents=True, exist_ok=True)
        append_private_lines(
            memory_dir / "memory_history.jsonl",
            [json.dumps(entry, ensure_ascii=False, sort_keys=True)],
        )
        return True
    except OSError:
        logger.warning("failed to append memory history", exc_info=True)
        return False


def _digest(text: str) -> str:
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_FAILURE_TAIL_BYTES = 65536
_FAILURE_ERROR_CHARS = 160


def recent_failure_streak(memory_dir: Path) -> tuple[int, str | None]:
    """Return the trailing run of failed automatic memory operations.

    A pipeline that keeps failing — an expired provider key answering 401 on
    every compile, an unreachable relay — otherwise starves silently behind
    per-attempt warnings.  The status probe surfaces the streak and the newest
    error (already sanitized by the writer) so a client can show that memory
    has stopped accumulating.  Reads only the tail of the audit log.
    """
    path = memory_dir / "memory_history.jsonl"
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _FAILURE_TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return 0, None
    streak = 0
    last_error: str | None = None
    for line in reversed(tail.splitlines()):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            # A torn trailing write, or the oldest record cut by the tail
            # window; neither decides anything, so skip rather than stop.
            continue
        if not isinstance(record, dict) or record.get("status") != "failed":
            break
        streak += 1
        if last_error is None:
            error = record.get("error")
            if isinstance(error, str) and error.strip():
                last_error = error.strip()[:_FAILURE_ERROR_CHARS]
    return streak, last_error


def trim_memory_history(
    memory_dir: Path,
    *,
    max_entries: int = _MAX_HISTORY_ENTRIES,
    max_age_days: int = _MAX_HISTORY_AGE_DAYS,
) -> bool:
    """Keep the audit history bounded: the most recent entries within an age.

    Called during Dream maintenance (low-frequency), so it never adds work to a
    per-turn hot path.  Returns whether the log was rewritten.
    """
    history_path = memory_dir / "memory_history.jsonl"
    try:
        lines = history_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    if not lines:
        return False

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    kept: list[str] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        try:
            timestamp = datetime.fromisoformat(str(entry.get("timestamp", "")))
        except (ValueError, TypeError):
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        if timestamp >= cutoff:
            kept.append(line)

    if len(kept) > max_entries:
        kept = kept[-max_entries:]
    if kept == lines:
        return False
    try:
        atomic_write_text(history_path, "".join(line + "\n" for line in kept))
    except OSError:
        logger.warning("failed to trim memory history", exc_info=True)
        return False
    return True
