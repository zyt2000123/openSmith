from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import sysconfig

PROJECT_ROOT_ENV = "AGENT_SMITH_PROJECT_ROOT"
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def _default_project_root() -> Path:
    configured_root = os.environ.get(PROJECT_ROOT_ENV)
    if configured_root:
        project_root = Path(configured_root).expanduser().resolve()
        if not (project_root / "agents").is_dir():
            raise RuntimeError(
                f"{PROJECT_ROOT_ENV} must point to an Agent-Smith root containing agents/"
            )
        return project_root

    source_root = Path(__file__).resolve().parent.parent
    if (source_root / "agents").is_dir():
        return source_root

    working_dir = Path.cwd().resolve()
    for candidate in (working_dir, *working_dir.parents):
        if (candidate / "agents").is_dir():
            return candidate

    return source_root


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    path.chmod(PRIVATE_DIR_MODE)


@dataclass(frozen=True)
class AppPaths:
    data_dir: Path
    project_root: Path

    @classmethod
    def defaults(cls) -> "AppPaths":
        return cls(
            data_dir=Path.home() / ".agent-smith",
            project_root=_default_project_root(),
        )

    @property
    def agent_dir(self) -> Path:
        return self.data_dir / "agent"

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "sqlite" / "agent-smith.sqlite"

    @property
    def smith_profile_dir(self) -> Path:
        return self.project_root / "agents" / "smith"

    @property
    def builtin_skills_dir(self) -> Path:
        return self.data_dir / "builtin" / "skills"

    @property
    def bundled_skills_dir(self) -> Path:
        """Skill assets shipped with Smith, with a source-tree fallback for development."""
        installed = Path(sysconfig.get_path("data")) / "agent_smith_common" / "builtin_skills"
        if installed.is_dir():
            return installed
        return self.project_root / "agents" / "skills"

    @property
    def builtin_tools_dir(self) -> Path:
        return self.project_root / "agents" / "tools"

    @property
    def builtin_identities_dir(self) -> Path:
        return self.project_root / "agents" / "identities"

    @property
    def safety_rules_path(self) -> Path:
        return self.project_root / "agents" / "safety" / "dangerous_commands.json"

    def ensure_base_dirs(self) -> None:
        _ensure_private_dir(self.data_dir)
        _ensure_private_dir(self.agent_dir)
        _ensure_private_dir(self.sqlite_path.parent)
        self._install_builtin_skills()

    def _install_builtin_skills(self) -> None:
        """Materialize Smith-owned skills outside the user-editable skill directory.

        ``agent/skills`` remains reserved for user-installed skills.  Keeping
        shipped skills under ``builtin/skills`` lets an installed Smith retain
        its default capabilities without treating them as user customizations.

        The shipped set is discovered from the bundled directory, so adding a
        skill there needs no second declaration in this layer.
        """
        source = self.bundled_skills_dir
        if not source.is_dir():
            return

        target = self.builtin_skills_dir
        _ensure_private_dir(target.parent)
        _ensure_private_dir(target)
        manifest = target / ".manifest.json"

        shipped = sorted(
            child.name for child in source.iterdir() if (child / "SKILL.md").is_file()
        )
        for name in shipped:
            shutil.copytree(source / name, target / name, dirs_exist_ok=True)

        for child in target.iterdir():
            if child.is_dir() and child.name not in shipped:
                shutil.rmtree(child)
        manifest.write_text(json.dumps({"skills": shipped}), encoding="utf-8")
        manifest.chmod(PRIVATE_FILE_MODE)
