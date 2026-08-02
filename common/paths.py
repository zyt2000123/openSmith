from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sysconfig
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT_ENV = "AGENT_SMITH_PROJECT_ROOT"
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

logger = logging.getLogger(__name__)


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

        # A project root must have every Smith runtime asset. Requiring only
        # one generic ``agents`` subdirectory can select an unrelated project.
        is_agent_smith = (
            (agents_dir / "smith" / "config.yaml").is_file()
            and (agents_dir / "identities" / "smith.yaml").is_file()
            and any((agents_dir / "skills").glob("*/SKILL.md"))
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
    if path.is_symlink():
        raise RuntimeError(f"Refusing to use symlinked private directory: {path}")
    if path.exists():
        if not path.is_dir():
            raise NotADirectoryError(f"Private runtime path is not a directory: {path}")
        return
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    path.chmod(PRIVATE_DIR_MODE)


def _ensure_real_descendant(root: Path, path: Path) -> None:
    """Reject symlinks anywhere in a managed target path."""
    try:
        parts = path.relative_to(root).parts
    except ValueError as exc:
        raise RuntimeError(f"Managed path escapes its root: {path}") from exc

    current = root
    if current.is_symlink():
        raise RuntimeError(f"Refusing to use symlinked managed path: {current}")
    for part in parts:
        current /= part
        if current.is_symlink():
            raise RuntimeError(f"Refusing to use symlinked managed path: {current}")


def _remove_managed_path(root: Path, path: Path) -> None:
    _ensure_real_descendant(root, path)
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _ensure_managed_directory(root: Path, path: Path) -> None:
    """Create a private directory tree, replacing conflicting regular files."""
    try:
        parts = path.relative_to(root).parts
    except ValueError as exc:
        raise RuntimeError(f"Managed path escapes its root: {path}") from exc

    current = root
    _ensure_real_descendant(root, current)
    for part in parts:
        current /= part
        _ensure_real_descendant(root, current)
        if current.exists() and not current.is_dir():
            _remove_managed_path(root, current)
        current.mkdir(exist_ok=True, mode=PRIVATE_DIR_MODE)
        current.chmod(PRIVATE_DIR_MODE)


def _prepare_managed_file(root: Path, path: Path) -> None:
    """Ensure a file target has real directory parents and no type conflict."""
    _ensure_managed_directory(root, path.parent)
    _ensure_real_descendant(root, path)
    if path.exists() and not path.is_file():
        _remove_managed_path(root, path)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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

        Incremental sync verifies every shipped file by SHA-256, so an altered
        managed skill is restored even if its size and timestamp were preserved.
        """
        source = self.bundled_skills_dir
        if not source.is_dir():
            return

        target = self.builtin_skills_dir
        _ensure_private_dir(target.parent)
        _ensure_private_dir(target)
        manifest_path = target / ".manifest.json"
        _prepare_managed_file(target, manifest_path)

        shipped = sorted(
            child.name for child in source.iterdir() if (child / "SKILL.md").is_file()
        )
        if not shipped and any(child.is_dir() for child in target.iterdir()):
            logger.warning(
                "refusing to replace installed builtin skills with an empty source: %s",
                source,
            )
            return

        current_manifest = {"skills": shipped, "files": {}}

        for name in shipped:
            source_skill = source / name
            target_skill = target / name
            _ensure_managed_directory(target, target_skill)

            # Incremental copy: only update changed files
            for source_file in source_skill.rglob("*"):
                if not source_file.is_file():
                    continue

                rel_path = source_file.relative_to(source)
                target_file = target / rel_path
                _prepare_managed_file(target, target_file)

                # Check if file needs update
                source_stat = source_file.stat()
                file_key = str(rel_path)
                source_digest = _file_digest(source_file)
                needs_update = (
                    not target_file.is_file()
                    or _file_digest(target_file) != source_digest
                )

                if needs_update:
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_file, target_file)
                    target_file.chmod(PRIVATE_FILE_MODE)

                # Record in new manifest
                current_manifest["files"][file_key] = {
                    "mtime": source_stat.st_mtime,
                    "size": source_stat.st_size,
                    "sha256": source_digest,
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
                    _remove_managed_path(target, path)

        # Remove obsolete skills
        for child in target.iterdir():
            if child.is_dir() and child.name not in shipped:
                _remove_managed_path(target, child)

        # Write new manifest
        manifest_path.write_text(
            json.dumps(current_manifest, indent=2), encoding="utf-8"
        )
        manifest_path.chmod(PRIVATE_FILE_MODE)
