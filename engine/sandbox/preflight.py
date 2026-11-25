"""Cross-platform multi-root hardlink preflight with directory-fingerprint cache.

Path-based sandbox profiles (Seatbelt SBPL regexes and bwrap bind mounts)
cannot distinguish a hardlink alias from an ordinary file.  Before invoking a
sandboxed command, this preflight inspects every writable root and fails closed
if any regular file with ``st_nlink > 1`` either:

1. Shares an inode with a protected path (`.env`, `.git/...`, credential dirs,
   or a non-delegable write root), or
2. Has more hardlinks than are accounted for across the sandbox's own writable
   roots (meaning at least one link reaches outside the sandbox boundary).
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterable
from pathlib import Path

from .policy import is_hardlink_protected_path


def _is_under_non_delegable(path: Path, non_delegable: tuple[Path, ...]) -> bool:
    if not non_delegable:
        return False
    folded_path = Path(str(path).lower())
    for root in non_delegable:
        try:
            if folded_path.is_relative_to(Path(str(root).lower())):
                return True
        except ValueError:
            continue
    return False


def compute_roots_fingerprint(roots: tuple[Path, ...]) -> str | None:
    """Cheap digest of directory layouts across ``roots``.

    Returns ``None`` when any directory cannot be inspected, forcing the full
    preflight walk to run (fail closed).
    """
    digest = hashlib.sha256()
    for base in roots:
        pending = [str(base)]
        while pending:
            current = pending.pop()
            try:
                metadata = os.lstat(current)
            except OSError:
                return None
            digest.update(os.fsencode(current))
            digest.update(str(metadata.st_mtime_ns).encode("ascii"))
            digest.update(str(metadata.st_ctime_ns).encode("ascii"))
            digest.update(str(metadata.st_size).encode("ascii"))
            try:
                with os.scandir(current) as entries:
                    child_dirs = sorted(
                        entry.path
                        for entry in entries
                        if entry.is_dir(follow_symlinks=False)
                    )
            except OSError:
                return None
            pending.extend(child_dirs)
    return digest.hexdigest()


def check_hardlinks(
    roots: tuple[Path, ...],
    non_delegable_write_paths: tuple[Path, ...] = (),
) -> str | None:
    """Inspect all writable ``roots`` for unsafe hardlink aliases."""
    # Keyed by (st_dev, st_ino) -> (list of (root, relative_path), declared st_nlink)
    seen: dict[tuple[int, int], tuple[list[tuple[Path, Path]], int]] = {}
    unique_roots: list[Path] = []
    for root in roots:
        if root not in unique_roots:
            unique_roots.append(root)

    for base in unique_roots:
        pending = [base]
        while pending:
            current = pending.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        try:
                            metadata = entry.stat(follow_symlinks=False)
                        except OSError:
                            return (
                                "sandbox protected-path preflight could not inspect workspace"
                            )
                        mode = metadata.st_mode
                        if stat.S_ISLNK(mode):
                            continue
                        if stat.S_ISDIR(mode):
                            pending.append(Path(entry.path))
                            continue
                        if not stat.S_ISREG(mode) or metadata.st_nlink <= 1:
                            continue
                        full_path = Path(entry.path)
                        try:
                            relative = full_path.relative_to(base)
                        except ValueError:
                            return (
                                "sandbox protected-path preflight could not inspect workspace"
                            )
                        key = (metadata.st_dev, metadata.st_ino)
                        entries_for_inode, _ = seen.setdefault(
                            key, ([], metadata.st_nlink)
                        )
                        entries_for_inode.append((base, relative))
            except OSError:
                return "sandbox protected-path preflight could not inspect workspace"

    for items, link_count in seen.values():
        aliases = ", ".join(
            sorted(
                str(rel) if len(unique_roots) == 1 else str(root / rel)
                for root, rel in items
            )
        )
        protected = next(
            (
                rel
                for root, rel in items
                if is_hardlink_protected_path(rel)
                or _is_under_non_delegable(root / rel, non_delegable_write_paths)
            ),
            None,
        )
        if protected is not None:
            return (
                "sandbox refuses files with hard links to a protected path "
                f"({protected}): {aliases}"
            )
        if link_count > len(items):
            return (
                "sandbox refuses files with hard links reaching outside the "
                f"workspace: {aliases} (copy the file, or re-create it "
                "without hard links, to run shell here)"
            )
    return None


class HardlinkPreflightCache:
    """Caches hardlink preflight outcomes keyed by multi-root directory fingerprint."""

    def __init__(self) -> None:
        self._cache: tuple[str, tuple[Path, ...], tuple[Path, ...], str | None] | None = None

    def clear(self) -> None:
        self._cache = None

    def check(
        self,
        roots: Iterable[Path],
        non_delegable_write_paths: Iterable[Path] = (),
        *,
        walk_fn=None,
    ) -> str | None:
        root_tuple = tuple(roots)
        nd_tuple = tuple(non_delegable_write_paths)
        fingerprint = compute_roots_fingerprint(root_tuple)
        cached = self._cache
        if (
            cached is not None
            and fingerprint is not None
            and cached[0] == fingerprint
            and cached[1] == root_tuple
            and cached[2] == nd_tuple
        ):
            return cached[3]
        error = (
            walk_fn()
            if walk_fn is not None
            else check_hardlinks(root_tuple, nd_tuple)
        )
        if fingerprint is not None:
            self._cache = (fingerprint, root_tuple, nd_tuple, error)
        return error


__all__ = (
    "HardlinkPreflightCache",
    "check_hardlinks",
    "compute_roots_fingerprint",
)
