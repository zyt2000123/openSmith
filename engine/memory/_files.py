"""Small filesystem primitives and shared utilities for memory writers."""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import AsyncIterator, BinaryIO, Iterator


# ---------------------------------------------------------------------------
# Canonical memory layer filenames
# ---------------------------------------------------------------------------

MEMORY_LAYER_FILES: tuple[str, ...] = ("durable.md", "recent.md")


def _lock_file_path(target: Path) -> Path:
    return target.with_name(f"{target.name}.lock")


def _acquire_lock_handle(target: Path) -> BinaryIO:
    lock_path = _lock_file_path(target)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        handle = os.fdopen(fd, "a+b")
    except BaseException:
        os.close(fd)
        raise

    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except BaseException:
        handle.close()
        raise
    return handle


def _release_lock_handle(handle: BinaryIO) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


@contextmanager
def interprocess_file_lock(target: Path) -> Iterator[None]:
    """Hold an advisory process-wide lock associated with ``target``."""
    handle = _acquire_lock_handle(target)
    try:
        yield
    finally:
        _release_lock_handle(handle)


@asynccontextmanager
async def async_interprocess_file_lock(target: Path) -> AsyncIterator[None]:
    """Acquire ``interprocess_file_lock`` without blocking the event loop."""
    acquisition = asyncio.create_task(
        asyncio.to_thread(_acquire_lock_handle, target)
    )
    try:
        handle = await asyncio.shield(acquisition)
    except asyncio.CancelledError:
        handle = await acquisition
        _release_lock_handle(handle)
        raise
    try:
        yield
    finally:
        _release_lock_handle(handle)


# ---------------------------------------------------------------------------
# Secret detection — shared by dream.py, store.py, memory_ops.py
# ---------------------------------------------------------------------------

SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?<![a-zA-Z])sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{20,}"),
    re.compile(r"""(?i)(?:api[_-]?key|password|secret|token|credential)["']?\s*[:=]\s*["']?\S{8,}"""),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{16,}"),
    re.compile(r"(?i)(?:postgres|mysql|mongodb|redis)://\S+:\S+@"),
    re.compile(r"-----BEGIN\s+(?:RSA\s+|EC\s+|DSA\s+|OPENSSH\s+|ENCRYPTED\s+)?PRIVATE\s+KEY-----"),
]


def contains_secret(text: str) -> bool:
    return any(p.search(text) for p in SECRET_PATTERNS)


_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)ignore\s+(?:all\s+)?previous\s+instructions"),
    # An article is required: "you are now a pirate" is a role change, while
    # "you are now ready to deploy" is ordinary prose that memory must keep.
    re.compile(r"(?i)you\s+(?:are|must)\s+now\s+(?:a|an|the)\s+\w+"),
    # A bare "system:" appears in ordinary notes ("system: darwin 25.5.0").
    # Only treat it as injection when it introduces an instruction.
    re.compile(
        r"(?i)(?:^|\s)system\s*:\s*(?:you\b|your\b|ignore\b|forget\b|"
        r"disregard\b|act\s+as\b|from\s+now\s+on\b|new\s+\w+\s*:)"
    ),
    re.compile(r"(?i)new\s+(?:system\s+)?(?:role|instruction|policy)"),
    re.compile(r"(?i)override\s+(?:your|the|all)\s+(?:instructions|rules|policy)"),
    re.compile(r"忽略(?:之前|上面|先前)(?:的)?(?:所有|全部)?(?:指令|指示|规则|提示)"),
]


def contains_injection(text: str) -> bool:
    """Deterministic heuristic for prompt-injection payloads in memory content."""
    normalized = re.sub(r"\s+", " ", text).strip()
    return any(p.search(normalized) for p in _INJECTION_PATTERNS)


def sanitize_memory_text(text: str) -> tuple[str, int, int]:
    """Remove unsafe lines before memory is persisted or injected into a prompt.

    Returns the cleaned text plus counts of removed secret and instruction-like
    lines.  Line-level removal preserves unrelated user-authored context while
    ensuring a known unsafe fragment cannot survive into the prompt layer.
    """
    lines = text.splitlines()
    clean: list[str] = []
    secrets_removed = 0
    injections_removed = 0
    for line in lines:
        if contains_secret(line):
            secrets_removed += 1
        elif contains_injection(line):
            injections_removed += 1
        else:
            clean.append(line)
    if contains_injection(text) and injections_removed == 0:
        return "", secrets_removed, 1
    return "\n".join(clean), secrets_removed, injections_removed


def safe_file_in_dir(root: Path, path: Path) -> Path | None:
    """Return a resolved file only when it stays under *root*."""
    try:
        resolved_root = root.resolve()
        resolved_path = path.resolve(strict=True)
    except OSError:
        return None
    if not resolved_path.is_relative_to(resolved_root):
        return None
    if not resolved_path.is_file():
        return None
    return resolved_path


def safe_markdown_files(directory: Path) -> list[Path]:
    """List markdown files without following symlinks outside *directory*."""
    if not directory.is_dir():
        return []
    files: list[Path] = []
    for path in sorted(directory.glob("*.md")):
        resolved = safe_file_in_dir(directory, path)
        if resolved is not None:
            files.append(resolved)
    return files


def atomic_write_text(path: Path, content: str) -> None:
    """Replace *path* atomically, keeping an existing file intact on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(temp_name)
    try:
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            # fdopen failed, so the raw fd is still ours to close exactly once.
            os.close(fd)
            raise
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
