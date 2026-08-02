from __future__ import annotations

import json
import os
import shutil
import sysconfig
from dataclasses import dataclass
from pathlib import Path

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

    # Stricter validation: check for Agent-Smith signature files
    # to avoid mistaking another project's agents/ directory
    working_dir = Path.cwd().resolve()
    for candidate in (working_dir, *working_dir.parents):
        agents_dir = candidate / "agents"
        if not agents_dir.is_dir():
            continue

        # Verify it's an Agent-Smith agents/ directory by checking for:
        # - smith/ identity directory
        # - skills/ directory with SKILL.md files
        # - identities/ directory
        is_agent_smith = (
            (agents_dir / "smith").is_dir()
            or (agents_dir / "identities").is_dir()
            or any((agents_dir / "skills").glob("*/SKILL.md"))
        )

        if is_agent_smith:
            return candidate

        # Log warning if we skip a candidate (helpful for debugging mismatches)
        import logging
        logging.getLogger(__name__).debug(
            f"Skipping {candidate}: has agents/ but missing Agent-Smith markers"
        )

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

        Incremental sync: only copies files that have changed (by mtime + size).
        """
        source = self.bundled_skills_dir
        if not source.is_dir():
            return

        target = self.builtin_skills_dir
        _ensure_private_dir(target.parent)
        _ensure_private_dir(target)
        manifest_path = target / ".manifest.json"

        # Load previous manifest to check what changed
        previous_manifest = {}
        if manifest_path.is_file():
            try:
                previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass  # Treat as first install

        shipped = sorted(
            child.name for child in source.iterdir() if (child / "SKILL.md").is_file()
        )

        current_manifest = {"skills": shipped, "files": {}}

        for name in shipped:
            source_skill = source / name
            target_skill = target / name
            target_skill.mkdir(parents=True, exist_ok=True)

            # Incremental copy: only update changed files
            for source_file in source_skill.rglob("*"):
                if not source_file.is_file():
                    continue

                rel_path = source_file.relative_to(source)
                target_file = target / rel_path

                # Check if file needs update
                source_stat = source_file.stat()
                file_key = str(rel_path)
                needs_update = True

                if file_key in previous_manifest.get("files", {}):
                    prev = previous_manifest["files"][file_key]
                    if (target_file.is_file()
                        and prev["mtime"] == source_stat.st_mtime
                        and prev["size"] == source_stat.st_size):
                        needs_update = False

                if needs_update:
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_file, target_file)
                    target_file.chmod(PRIVATE_FILE_MODE)

                # Record in new manifest
                current_manifest["files"][file_key] = {
                    "mtime": source_stat.st_mtime,
                    "size": source_stat.st_size,
                }

            # Prune stale files in this skill
            if target_skill.is_dir():
                stale_paths = sorted(
                    (
                        path
                        for path in target_skill.rglob("*")
                        if not (source_skill / path.relative_to(target_skill)).exists()
                    ),
                    key=lambda path: len(path.parts),
                    reverse=True,
                )
                for path in stale_paths:
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                    else:
                        path.unlink(missing_ok=True)

        # Remove obsolete skills
        for child in target.iterdir():
            if child.is_dir() and child.name not in shipped:
                shutil.rmtree(child)

        # Write new manifest
        manifest_path.write_text(
            json.dumps(current_manifest, indent=2), encoding="utf-8"
        )
        manifest_path.chmod(PRIVATE_FILE_MODE)
