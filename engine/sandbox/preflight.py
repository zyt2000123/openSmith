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
    *,
    cache_roots: tuple[Path, ...] = (),
    runtime_secret_paths: tuple[Path, ...] = (),
) -> str | None:
    """Inspect all writable ``roots`` (and ``cache_roots``) for unsafe hardlink aliases.

    Workspace roots (``roots``) enforce both protected-path aliasing and the
    unaccounted external-link invariant (``st_nlink > seen_aliases``).  Package
    ``cache_roots`` (e.g. ``~/.cache/uv``, pnpm store) legitimately share
    content-addressed hardlinks with other projects' virtual environments, so
    for cache-only inodes we check against protected paths, runtime secrets,
    and non-delegable write roots without false-positiving on cross-repo cache
    links.
    """
    # Keyed by (st_dev, st_ino) -> (list of (root, relative_path, is_cache), declared st_nlink)
    seen: dict[tuple[int, int], tuple[list[tuple[Path, Path, bool]], int]] = {}
    unique_workspace_roots: list[Path] = []
    for root in roots:
        if root not in unique_workspace_roots:
            unique_workspace_roots.append(root)
    unique_cache_roots: list[Path] = []
    for root in cache_roots:
        if root not in unique_workspace_roots and root not in unique_cache_roots:
            unique_cache_roots.append(root)

    protected_identities: set[tuple[int, int]] = set()
    for secret_path in runtime_secret_paths:
        try:
            st = os.lstat(secret_path)
            if stat.S_ISREG(st.st_mode):
                protected_identities.add((st.st_dev, st.st_ino))
        except OSError:
            continue

    scan_plan = [
        *((root, False) for root in unique_workspace_roots),
        *((root, True) for root in unique_cache_roots),
    ]
    total_roots = len(scan_plan)

    for base, is_cache in scan_plan:
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
                        entries_for_inode.append((base, relative, is_cache))
            except OSError:
                return "sandbox protected-path preflight could not inspect workspace"

    for key, (items, link_count) in seen.items():
        aliases = ", ".join(
            sorted(
                str(rel) if total_roots == 1 else str(root / rel)
                for root, rel, _ in items
            )
        )
        if key in protected_identities:
            return (
                "sandbox refuses files with hard links to a protected path "
                f"(runtime secret): {aliases}"
            )
        protected = next(
            (
                rel
                for root, rel, _ in items
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
        has_workspace_alias = any(not is_cache for _, _, is_cache in items)
        has_cache_alias = any(is_cache for _, _, is_cache in items)
        if has_workspace_alias and not has_cache_alias and link_count > len(items):
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
