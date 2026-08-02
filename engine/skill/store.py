"""Version-controlled skill storage for agent-installed skills.

Each agent's skills live under the agent profile's skills dir (…/<id>/skills/).
Layout per skill:

    skills/<name>/SKILL.md          # current version
    skills/<name>/.versions/        # timestamped snapshots
        20260704T120000.md
        20260704T130000.md
        ...

Only the last 10 versions are kept.
"""

from __future__ import annotations

import asyncio
import difflib
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_MAX_VERSIONS = 10


def _atomic_write_text(path: Path, content: str) -> None:
    """Write *content* to *path* atomically (temp file + ``os.replace``).

    A crash between truncate and write must not leave a partial SKILL.md or
    version snapshot on disk.
    """
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _safe_skill_name(skill_name: str) -> str:
    """Return one path component, rejecting traversal rather than rewriting it."""
    safe = Path(skill_name).name
    if not skill_name or safe != skill_name or safe in {".", ".."}:
        raise ValueError("skill name must be a single non-relative path component")
    return safe


class SkillStore:
    """Version-controlled skill storage for agent-installed skills."""

    def __init__(self, skills_dir: Path) -> None:
        skills_dir = Path(skills_dir)
        if skills_dir.is_symlink():
            raise ValueError("skills root must not be a symlink")
        self._dir = skills_dir.resolve()  # agent profile skills dir: …/<id>/skills/

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _skill_dir(self, skill_name: str) -> Path:
        safe = _safe_skill_name(skill_name)
        path = self._dir / safe
        if path.is_symlink() or not path.resolve(strict=False).is_relative_to(self._dir):
            raise ValueError("skill directory escapes skills root")
        return path

    def _skill_file(self, skill_name: str) -> Path:
        path = self._skill_dir(skill_name) / "SKILL.md"
        if path.is_symlink():
            raise ValueError("skill file must not be a symlink")
        return path

    def _versions_dir(self, skill_name: str) -> Path:
        path = self._skill_dir(skill_name) / ".versions"
        if path.is_symlink():
            raise ValueError("skill versions directory must not be a symlink")
        return path

    def _prune(self, skill_name: str) -> None:
        """Keep only the last _MAX_VERSIONS snapshots."""
        vdir = self._versions_dir(skill_name)
        if not vdir.is_dir():
            return
        versions = sorted(vdir.glob("*.md"))
        while len(versions) > _MAX_VERSIONS:
            versions.pop(0).unlink()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _save_version_sync(self, skill_name: str, content: str) -> str:
        """Save current SKILL.md as a numbered version, return version id.

        *content* is the text that is about to be replaced (the old version).
        """
        vdir = self._versions_dir(skill_name)
        vdir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        version_id = ts
        target = vdir / f"{version_id}.md"

        # Avoid collision (unlikely but safe)
        seq = 0
        while target.exists():
            seq += 1
            version_id = f"{ts}_{seq}"
            target = vdir / f"{version_id}.md"

        _atomic_write_text(target, content)
        self._prune(skill_name)
        return version_id

    async def save_version(self, skill_name: str, content: str) -> str:
        return await asyncio.to_thread(self._save_version_sync, skill_name, content)

    def _rollback_sync(self, skill_name: str, version_id: str) -> bool:
        """Restore a previous version of a skill."""
        safe_vid = Path(version_id).name
        if not version_id or safe_vid != version_id or safe_vid in {".", ".."}:
            return False
        snapshot = self._versions_dir(skill_name) / f"{safe_vid}.md"
        if snapshot.is_symlink() or not snapshot.is_file():
            return False

        skill_file = self._skill_file(skill_name)
        if not skill_file.is_file():
            return False

        # Read the snapshot *before* saving/pruning current: at _MAX_VERSIONS
        # the prune triggered by _save_version_sync deletes the oldest
        # snapshot — the very file we are about to restore.  Guard the read so
        # a vanished snapshot is a False result, never a raised exception.
        try:
            snapshot_content = snapshot.read_text(encoding="utf-8")
        except OSError:
            return False

        # Save current as a version before rolling back, unless it already
        # matches the snapshot being restored.  Skipping the identical save
        # keeps repeated rollbacks from growing the version set.
        try:
            current = skill_file.read_text(encoding="utf-8")
        except OSError:
            return False
        if current != snapshot_content:
            self._save_version_sync(skill_name, current)

        # Restore atomically so a crash cannot leave a partial SKILL.md.
        _atomic_write_text(skill_file, snapshot_content)
        return True

    async def rollback(self, skill_name: str, version_id: str) -> bool:
        return await asyncio.to_thread(self._rollback_sync, skill_name, version_id)

    def _list_versions_sync(self, skill_name: str) -> list[dict]:
        """List available versions with timestamps."""
        vdir = self._versions_dir(skill_name)
        if not vdir.is_dir():
            return []

        result: list[dict] = []
        for f in sorted(vdir.glob("*.md")):
            result.append({
                "version_id": f.stem,
                "timestamp": f.stem.replace("T", " "),
                "size": f.stat().st_size,
            })
        return result

    async def list_versions(self, skill_name: str) -> list[dict]:
        return await asyncio.to_thread(self._list_versions_sync, skill_name)

    def _diff_sync(self, skill_name: str, v1: str, v2: str) -> str:
        """Show unified diff between two versions.

        v1/v2 can be version ids or the special value "current" for the
        live SKILL.md.
        """
        def _read(vid: str) -> list[str]:
            if vid == "current":
                p = self._skill_file(skill_name)
            else:
                safe = Path(vid).name
                if not vid or safe != vid or safe in {".", ".."}:
                    raise FileNotFoundError(f"Version '{vid}' not found")
                p = self._versions_dir(skill_name) / f"{safe}.md"
            if p.is_symlink() or not p.is_file():
                raise FileNotFoundError(f"Version '{vid}' not found")
            return p.read_text(encoding="utf-8").splitlines(keepends=True)

        lines_a = _read(v1)
        lines_b = _read(v2)

        diff_lines = difflib.unified_diff(
            lines_a, lines_b,
            fromfile=f"{skill_name}@{v1}",
            tofile=f"{skill_name}@{v2}",
        )
        result = "".join(diff_lines)
        return result if result else "(no differences)"

    async def diff(self, skill_name: str, v1: str, v2: str) -> str:
        return await asyncio.to_thread(self._diff_sync, skill_name, v1, v2)
