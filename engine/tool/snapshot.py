"""Tool-side file snapshots for backup and rewind."""

from __future__ import annotations

import hashlib
import os
import shutil
import re
from pathlib import Path

# ponytail: track() runs on every write_file/edit_file, so without a cap the
# per-file version list (and backup files on disk) grow unbounded per session.
_MAX_VERSIONS_PER_FILE = 20
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _safe_session_id(session_id: str) -> str:
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("session_id must contain only letters, digits, underscores, or hyphens")
    return session_id


class FileSnapshot:
    """Track and backup files before they are modified by Agent tools.

    Usage:
        snap = FileSnapshot(session_id)
        snap.track(path)         # call BEFORE write_file/edit
        snap.rewind(path)        # restore to pre-modification state
    """

    def __init__(self, session_id: str = "default"):
        session_id = _safe_session_id(session_id)
        try:
            from common.config import DATA_DIR
            self._backup_dir = DATA_DIR / "snapshots" / session_id
        except Exception:
            self._backup_dir = Path.home() / ".agent-smith" / "snapshots" / session_id
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        self._tracked: dict[str, list[str]] = {}
        # Monotonic per-file version counter. Never derived from
        # len(versions): pruning shrinks the list, and reusing a version
        # number would overwrite a backup file that is still referenced.
        self._version_seq: dict[str, int] = {}

    def _backup_name(self, filepath: str, version: int) -> str:
        h = hashlib.sha256(filepath.encode()).hexdigest()[:16]
        return f"{h}_v{version}"

    def track(self, filepath: str) -> bool:
        """Backup a file before it gets modified. Returns True if backup was created."""
        resolved = os.path.realpath(filepath)
        if not os.path.isfile(resolved):
            self._tracked.setdefault(resolved, []).append("")
            self._prune(resolved)
            return True

        version = self._version_seq.get(resolved, 0) + 1
        backup_name = self._backup_name(resolved, version)
        backup_path = self._backup_dir / backup_name

        try:
            shutil.copy2(resolved, backup_path)
        except Exception:
            return False
        self._version_seq[resolved] = version
        self._tracked.setdefault(resolved, []).append(backup_name)
        self._prune(resolved)
        return True

    def _prune(self, resolved: str) -> None:
        # ponytail: keep only the newest _MAX_VERSIONS_PER_FILE backups per
        # file; delete the pruned backup files from disk as well.
        versions = self._tracked.get(resolved)
        if not versions:
            return
        while len(versions) > _MAX_VERSIONS_PER_FILE:
            oldest = versions.pop(0)
            if not oldest:
                continue  # "" marks "file did not exist" — nothing on disk
            try:
                (self._backup_dir / oldest).unlink(missing_ok=True)
            except OSError:
                pass

    def rewind(self, filepath: str) -> bool:
        """Restore a file to its state before the last modification."""
        resolved = os.path.realpath(filepath)
        versions = self._tracked.get(resolved, [])
        if not versions:
            return False

        backup_name = versions[-1]
        if not backup_name:
            if os.path.exists(resolved):
                os.remove(resolved)
            return True

        backup_path = self._backup_dir / backup_name
        if not backup_path.is_file():
            return False

        try:
            os.makedirs(os.path.dirname(resolved), exist_ok=True)
            shutil.copy2(str(backup_path), resolved)
            return True
        except Exception:
            return False

_active_snapshots: dict[str, FileSnapshot] = {}


def get_snapshot(session_id: str = "default") -> FileSnapshot:
    session_id = _safe_session_id(session_id)
    if session_id not in _active_snapshots:
        _active_snapshots[session_id] = FileSnapshot(session_id)
    return _active_snapshots[session_id]
