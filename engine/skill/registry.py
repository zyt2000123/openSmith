from __future__ import annotations

import logging
from pathlib import Path
from threading import RLock

from .loader import SkillBody, parse_skill_md

logger = logging.getLogger(__name__)


def _parse_or_skip(skill_file: Path) -> SkillBody | None:
    """Parse one SKILL.md, isolating failures so one broken skill cannot
    prevent the rest from loading."""
    try:
        return parse_skill_md(skill_file)
    except Exception:
        logger.warning("Skipping unparseable skill: %s", skill_file, exc_info=True)
        return None


class SkillRegistry:
    def __init__(self) -> None:
        self._lock = RLock()
        self._skills: dict[str, SkillBody] = {}
        self._builtin_skills: dict[str, SkillBody] = {}
        self._agent_skills: dict[str, SkillBody] = {}
        self._builtin_names: set[str] = set()
        self._agent_names: set[str] = set()
        self._agent_skills_dir: Path | None = None
        self._agent_skill_dirs: dict[str, Path] = {}
        self._allowed_names: set[str] | None = None

    def _rebuild_active_catalog(self) -> None:
        skills = dict(self._builtin_skills)
        builtin_names = set(skills)
        for name, skill in self._agent_skills.items():
            skills[name] = skill
            builtin_names.discard(name)

        if self._allowed_names is not None:
            skills = {
                name: skill
                for name, skill in skills.items()
                if name in self._allowed_names
            }
            builtin_names.intersection_update(self._allowed_names)

        self._skills = skills
        self._builtin_names = builtin_names
        self._agent_names = set(skills).difference(builtin_names)

    def load_builtin(self, skills_dir: Path) -> None:
        """Scan *skills_dir* for subdirectories containing SKILL.md."""
        if skills_dir.is_symlink() or not skills_dir.is_dir():
            return
        resolved_root = skills_dir.resolve()
        loaded: dict[str, SkillBody] = {}
        for child in sorted(skills_dir.iterdir()):
            if child.is_symlink() or not child.is_dir():
                continue
            skill_file = child / "SKILL.md"
            if (
                skill_file.is_symlink()
                or not skill_file.is_file()
                or not skill_file.resolve().is_relative_to(resolved_root)
            ):
                continue
            skill = _parse_or_skip(skill_file)
            if skill is None:
                continue
            if skill.meta.name != child.name:
                logger.warning(
                    "Skipping skill whose directory and declared name differ: %s",
                    skill_file,
                )
                continue
            loaded[skill.meta.name] = skill
        with self._lock:
            self._builtin_skills = loaded
            # A fresh scan supersedes any prior per-request allowlist; a stale
            # _allowed_names would otherwise hide newly added skills after a
            # mid-request catalog refresh.
            self._allowed_names = None
            self._rebuild_active_catalog()

    def load_agent_skills(self, agent_skills_dir: Path) -> None:
        """Atomically replace the agent-specific skill catalog."""
        if agent_skills_dir.is_symlink() or not agent_skills_dir.is_dir():
            with self._lock:
                self._agent_skills_dir = None
                self._agent_skills = {}
                self._agent_skill_dirs = {}
                # See comment below: a fresh scan must not be gated by a stale
                # per-request allowlist.
                self._allowed_names = None
                self._rebuild_active_catalog()
            return
        resolved_root = agent_skills_dir.resolve()
        loaded: dict[str, SkillBody] = {}
        loaded_dirs: dict[str, Path] = {}
        for child in sorted(agent_skills_dir.iterdir()):
            if child.is_symlink() or not child.is_dir():
                continue
            skill_file = child / "SKILL.md"
            if (
                skill_file.is_symlink()
                or not skill_file.is_file()
                or not skill_file.resolve().is_relative_to(resolved_root)
            ):
                continue
            skill = _parse_or_skip(skill_file)
            if skill is None:
                continue
            if skill.meta.name != child.name:
                logger.warning(
                    "Skipping agent skill whose directory and declared name differ: %s",
                    skill_file,
                )
                continue
            loaded[skill.meta.name] = skill
            loaded_dirs[skill.meta.name] = child
        with self._lock:
            self._agent_skills_dir = agent_skills_dir
            self._agent_skills = loaded
            self._agent_skill_dirs = loaded_dirs
            # Mid-request refreshes (e.g. after a skill_manage mutation) must
            # re-derive the active catalog from the full scan.  A stale
            # _allowed_names set at request start would otherwise filter out
            # skills created after that point for the rest of the request;
            # callers re-apply their restrictions from the full catalog.
            self._allowed_names = None
            self._rebuild_active_catalog()

    def get(self, name: str) -> SkillBody | None:
        with self._lock:
            return self._skills.get(name)

    def is_builtin(self, name: str) -> bool:
        """Return True if the skill is a built-in (read-only) skill."""
        with self._lock:
            return name in self._builtin_names

    def get_agent_skill_dir(self, name: str) -> Path | None:
        """Return the path to an agent-installed skill's directory, or None."""
        with self._lock:
            if self._agent_skills_dir is None:
                return None
            if self._agent_skills_dir.is_symlink():
                return None
            if name in self._builtin_names or name not in self._agent_names:
                return None
            if Path(name).name != name:  # reject path traversal (e.g. "../x")
                return None
            skill_dir = self._agent_skill_dirs.get(name)
            if skill_dir is None:
                return None
            skill_file = skill_dir / "SKILL.md"
            if (
                skill_dir.is_dir()
                and not skill_dir.is_symlink()
                and skill_file.is_file()
                and not skill_file.is_symlink()
                and skill_dir.resolve().is_relative_to(self._agent_skills_dir.resolve())
            ):
                return skill_dir
        return None

    def restrict_to(self, names: tuple[str, ...] | list[str] | set[str]) -> None:
        """Restrict one per-request registry to an identity's declared skills."""
        allowed = set(names)
        with self._lock:
            if self._allowed_names is None:
                self._allowed_names = allowed
            else:
                self._allowed_names.intersection_update(allowed)
            self._rebuild_active_catalog()

    def list_summaries(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "name": s.meta.name,
                    "description": s.meta.description,
                    "source": "builtin" if s.meta.name in self._builtin_names else "agent",
                }
                for s in self._skills.values()
            ]
