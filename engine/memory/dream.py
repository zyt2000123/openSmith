"""Dream — low-frequency memory hygiene.

Runs every ~50 qualifying memory turns. Responsibilities:
  1. Sanitize: scan all rendered memory layers for secrets and injection markers
  2. Cleanup: reclaim the expired evidence prefix the compiler already consumed

Dream no longer reconciles durable.md. compile_durable() merges evidence into
the existing document on every run, so a second reconciler would re-review the
same facts with a second LLM pass and no new information.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ._snapshot import snapshot_views
from .compile import _read_offset
from ._files import (
    MEMORY_LAYER_FILES,
    atomic_write_text,
    safe_file_in_dir,
    safe_markdown_files,
    sanitize_memory_text,
)
from .history import append_memory_history
from .policy import MemoryPolicyError, load_memory_policy, resolve_view_path

if TYPE_CHECKING:
    from engine.llm.port import LLMPort


logger = logging.getLogger(__name__)
_MEMORY_POLICY = load_memory_policy()
_DREAM_CLEANUP_FILE = ".dream_cleanup.json"
# How long consumed evidence stays in recent.jsonl before Dream may reclaim it.
# This is a retention window on the raw event log only; durable.md itself never
# expires.
EVIDENCE_RETENTION_DAYS = 7


def _sanitize_lines(content: str) -> tuple[str, int, int]:
    """Remove secret and instruction-like lines with separate audit counts."""
    return sanitize_memory_text(content)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass
class DreamReport:
    secrets_removed: int = 0
    injection_lines_removed: int = 0
    log_lines_cleaned: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DreamCleanup:
    """A pending source-log replacement and its post-cleanup checkpoints."""

    cleaned: int
    old_recent_hash: str
    new_recent_hash: str
    compile_offset: int
    context_offset: int = 0


def dream_report_completed(report: DreamReport) -> bool:
    """Return whether Dream maintenance should reset its retry counter."""
    return not report.errors


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

DREAM_INTERVAL = 50


async def run_dream(
    memory_dir: Path,
    llm: "LLMPort",
    reviewer: "LLMPort | None" = None,
) -> DreamReport:
    """Sanitize the rendered memory files, then reclaim consumed event evidence."""
    report = DreamReport()

    cleanup_recovery_error = _recover_dream_cleanup(memory_dir)
    if cleanup_recovery_error:
        error = f"cleanup recovery: {cleanup_recovery_error}"
        report.errors.append(error)
        _record_dream_failure(memory_dir, error)
        logger.warning("dream cleanup recovery failed: %s", cleanup_recovery_error)
        return report

    try:
        _sanitize_all_layers(memory_dir, report)
    except Exception as exc:
        error = f"sanitation: {type(exc).__name__}: {exc}"
        report.errors.append(error)
        _record_dream_failure(memory_dir, error)
        logger.warning("dream sanitation failed", exc_info=True)
        return report

    try:
        _cleanup_log(memory_dir, report)
    except Exception as exc:
        error = f"cleanup: {type(exc).__name__}: {exc}"
        report.errors.append(error)
        _record_dream_failure(memory_dir, error)
        logger.warning("dream evidence cleanup failed", exc_info=True)

    if not report.errors:
        append_memory_history(
            memory_dir,
            target="dream",
            policy_version=_MEMORY_POLICY.version,
            status=(
                "written"
                if report.secrets_removed or report.injection_lines_removed
                else "unchanged"
            ),
        )

    return report


def _sanitize_all_layers(memory_dir: Path, report: DreamReport) -> None:
    """Scan all memory files for secrets and instruction-like content.

    Sanitizing is the second writer of the rendered views, and the only one that
    removes content without evidence -- legitimately so, since a leaked secret
    must go.  It therefore records the write and snapshots afterwards rather than
    being gated: without a trace, a false-positive secret match deletes real
    memory silently and nothing distinguishes it from a compiler deletion.
    """
    secrets_removed = 0
    injections_removed = 0
    sanitized_files = 0
    # Deliberately no pre-write snapshot here, unlike every other writer: the
    # text being replaced is the text that contains the leaked secret, and
    # committing it would park that secret in git history permanently -- the one
    # outcome sanitizing exists to prevent. Recovering from a false-positive
    # match uses the compiler's snapshots instead, which are clean by
    # construction because _commit_view refuses a draft containing secrets.
    for md_file in _all_memory_files(memory_dir):
        content = md_file.read_text(encoding="utf-8")
        cleaned, file_secrets, file_injections = _sanitize_lines(content)
        if file_secrets or file_injections:
            if not cleaned.strip() and content.strip():
                # A cross-line match made sanitize_memory_text wipe the whole
                # file (it returns "" when a secret/injection only matches the
                # full text).  Never blank a non-empty memory layer over a
                # single ambiguous match — that is a data-loss footgun.
                # Keep the original bytes and record the skip instead.
                logger.warning(
                    "refusing to blank non-empty memory layer %s during "
                    "sanitize (cross-line secret/injection match)",
                    md_file,
                )
                continue
            atomic_write_text(md_file, cleaned)
            secrets_removed += file_secrets
            injections_removed += file_injections
            sanitized_files += 1
            append_memory_history(
                memory_dir,
                target=md_file.name,
                policy_version=_MEMORY_POLICY.version,
                status="sanitized",
                old_text=content,
                new_text=cleaned,
            )
    if sanitized_files:
        snapshot_views(
            memory_dir.parent,
            f"memory: sanitized ({sanitized_files} file(s), "
            f"{secrets_removed} secret(s), {injections_removed} injection(s))",
        )
    report.secrets_removed = secrets_removed
    report.injection_lines_removed = injections_removed


def _record_dream_failure(memory_dir: Path, error: str) -> None:
    """Persist non-consolidation Dream failures that would otherwise be ephemeral."""
    append_memory_history(
        memory_dir,
        target="dream",
        policy_version=_MEMORY_POLICY.version,
        status="failed",
        error=error,
    )


def _all_memory_files(memory_dir: Path) -> list[Path]:
    """Collect all .md files across memory layers."""
    files = []
    try:
        context_path = resolve_view_path(_MEMORY_POLICY, memory_dir.parent, "context")
        safe_context = safe_file_in_dir(memory_dir.parent, context_path)
        if safe_context is not None:
            files.append(safe_context)
    except MemoryPolicyError:
        logger.warning("skipping context.md outside the Agent profile")
    for name in MEMORY_LAYER_FILES:
        path = memory_dir / name
        safe_path = safe_file_in_dir(memory_dir, path)
        if safe_path is not None:
            files.append(safe_path)
    episodes_dir = memory_dir / "episodes"
    files.extend(safe_markdown_files(episodes_dir))
    return files


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_dream_cleanup(memory_dir: Path, cleanup: DreamCleanup) -> None:
    """Journal a source-log replacement before changing any checkpoint."""
    atomic_write_text(
        memory_dir / _DREAM_CLEANUP_FILE,
        json.dumps(
            {
                "cleaned": cleanup.cleaned,
                "old_recent_hash": cleanup.old_recent_hash,
                "new_recent_hash": cleanup.new_recent_hash,
                "compile_offset": cleanup.compile_offset,
                "context_offset": cleanup.context_offset,
            },
            sort_keys=True,
        ),
    )


def _load_dream_cleanup(memory_dir: Path) -> tuple[DreamCleanup | None, str | None]:
    """Load a trusted pending Dream cleanup without following unsafe links."""
    path = memory_dir / _DREAM_CLEANUP_FILE
    if not path.exists() and not path.is_symlink():
        return None, None
    safe_path = safe_file_in_dir(memory_dir, path)
    if safe_path is None:
        return None, "Dream cleanup is unavailable or unsafe"
    try:
        payload = json.loads(safe_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None, "Dream cleanup could not be read"
    if not isinstance(payload, dict):
        return None, "Dream cleanup has an invalid format"

    cleaned = payload.get("cleaned")
    old_recent_hash = payload.get("old_recent_hash")
    new_recent_hash = payload.get("new_recent_hash")
    compile_offset = payload.get("compile_offset")
    # A journal written before context.md had its own cursor has no
    # ``context_offset``.  Recovering it as 0 makes context re-read the trimmed
    # log from the start: redundant work, never lost evidence -- and the journal
    # only survives at all if a crash landed inside a millisecond-wide window.
    context_offset = payload.get("context_offset", 0)
    # A journal written before the memory views were merged also carries
    # durable/dream/nudge offsets.  Those lanes no longer exist; ignore the
    # extra keys rather than rejecting a journal that must still be recovered.
    if (
        isinstance(cleaned, bool)
        or not isinstance(cleaned, int)
        or cleaned <= 0
        or not isinstance(old_recent_hash, str)
        or not isinstance(new_recent_hash, str)
        or len(old_recent_hash) != 64
        or len(new_recent_hash) != 64
        or isinstance(compile_offset, bool)
        or not isinstance(compile_offset, int)
        or compile_offset < 0
        or isinstance(context_offset, bool)
        or not isinstance(context_offset, int)
        or context_offset < 0
    ):
        return None, "Dream cleanup has invalid fields"
    return (
        DreamCleanup(
            cleaned=cleaned,
            old_recent_hash=old_recent_hash,
            new_recent_hash=new_recent_hash,
            compile_offset=compile_offset,
            context_offset=context_offset,
        ),
        None,
    )


def _clear_dream_cleanup(memory_dir: Path) -> None:
    (memory_dir / _DREAM_CLEANUP_FILE).unlink(missing_ok=True)


def _write_dream_cleanup_offsets(memory_dir: Path, cleanup: DreamCleanup) -> None:
    """Apply journaled post-cleanup checkpoints idempotently.

    Both cursors are rebased. Trimming lines off the front of the log shifts
    every position, so a cursor left un-rebased would point past evidence that
    moved down -- silently skipping it.
    """
    atomic_write_text(memory_dir / ".compile_offset", str(cleanup.compile_offset))
    atomic_write_text(
        memory_dir / ".compile_offset_context", str(cleanup.context_offset)
    )


def _recover_dream_cleanup(memory_dir: Path) -> str | None:
    """Finish an interrupted log cleanup without re-sending audited evidence."""
    cleanup, cleanup_error = _load_dream_cleanup(memory_dir)
    if cleanup_error or cleanup is None:
        return cleanup_error

    recent = safe_file_in_dir(memory_dir, memory_dir / "recent.jsonl")
    if recent is None:
        return "recent evidence is unavailable during Dream cleanup recovery"
    try:
        current_hash = _text_hash(recent.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return "recent evidence could not be read during Dream cleanup recovery"

    if current_hash == cleanup.old_recent_hash:
        # The journal reached disk but the source-log replacement did not.
        try:
            _clear_dream_cleanup(memory_dir)
        except OSError:
            return "stale Dream cleanup could not be cleared"
        return None
    if current_hash != cleanup.new_recent_hash:
        return "recent evidence changed during Dream cleanup recovery"

    try:
        _write_dream_cleanup_offsets(memory_dir, cleanup)
        _clear_dream_cleanup(memory_dir)
    except OSError as exc:
        return f"Dream cleanup checkpoints could not be recovered: {exc}"
    return None


def _cleanup_log(memory_dir: Path, report: DreamReport) -> int:
    """Remove only a contiguous expired prefix the compiler has already consumed."""
    recent_path = memory_dir / "recent.jsonl"
    recent = safe_file_in_dir(memory_dir, recent_path)
    offset_file = memory_dir / ".compile_offset"

    # A present-but-unsafe path (a symlink escaping memory_dir) must fail loudly.
    # Collapsing it into the "no log yet" branch would silently stop reclaiming
    # evidence while an attacker-controlled target sits in its place.
    if recent is None and (recent_path.exists() or recent_path.is_symlink()):
        raise OSError("recent.jsonl is unavailable or unsafe")
    if recent is None or not offset_file.is_file():
        return 0

    if safe_file_in_dir(memory_dir, memory_dir / "durable.md") is None:
        return 0

    # Both views consume the log, at their own pace. The reclaimable region ends
    # at whichever is further behind: durable's cursor says nothing about what
    # context has read, so trusting it alone deletes evidence one view never saw.
    compile_offset = _read_offset(memory_dir)
    context_offset = _read_offset(memory_dir, "context")
    offset = min(compile_offset, context_offset)

    if offset <= 0:
        return 0

    source_text = recent.read_text(encoding="utf-8")
    lines = source_text.strip().splitlines()
    if not lines:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=EVIDENCE_RETENTION_DAYS)
    safe_offset = 0
    for i, line in enumerate(lines[:offset]):
        try:
            entry = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            safe_offset = i + 1
            continue
        if not isinstance(entry, dict):
            safe_offset = i + 1
            continue
        timestamp = entry.get("timestamp")
        if not isinstance(timestamp, str) or not timestamp:
            break
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            break
        if parsed_timestamp.tzinfo is None:
            parsed_timestamp = parsed_timestamp.replace(tzinfo=timezone.utc)
        if parsed_timestamp.astimezone(timezone.utc) >= cutoff:
            break
        safe_offset = i + 1

    if safe_offset <= 0:
        return 0

    remaining = lines[safe_offset:]
    remaining_text = "\n".join(remaining) + "\n" if remaining else ""
    cleanup = DreamCleanup(
        cleaned=safe_offset,
        old_recent_hash=_text_hash(source_text),
        new_recent_hash=_text_hash(remaining_text),
        compile_offset=max(0, compile_offset - safe_offset),
        context_offset=max(0, context_offset - safe_offset),
    )
    _write_dream_cleanup(memory_dir, cleanup)
    # The cleanup journal below makes the truncation *atomic* -- it replays a
    # half-finished reclaim after a crash. It does not make it *undoable*: the
    # reclaimed prefix is gone once it is written out. Snapshot first so an
    # over-eager reclaim can be rolled back with the views that cite it.
    snapshot_views(memory_dir.parent, f"memory: before reclaiming {safe_offset} event(s)")
    atomic_write_text(recent, remaining_text)
    _write_dream_cleanup_offsets(memory_dir, cleanup)
    _clear_dream_cleanup(memory_dir)
    snapshot_views(memory_dir.parent, f"memory: reclaimed {safe_offset} event(s)")
    report.log_lines_cleaned = safe_offset
    return safe_offset
