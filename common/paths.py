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


def _is_agent_smith_root(project_root: Path) -> bool:
    agents_dir = project_root / "agents"
    return (
        (agents_dir / "smith" / "config.yaml").is_file()
        and (agents_dir / "identities" / "smith.yaml").is_file()
        and any((agents_dir / "skills").glob("*/SKILL.md"))
    )


def _ensure_real_path(path: Path, *, label: str = "path") -> None:
    """Reject a symlink at any existing component of a filesystem path."""
    absolute_path = path if path.is_absolute() else Path.cwd() / path
    current = Path(absolute_path.anchor)
    for part in absolute_path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise RuntimeError(f"Refusing to use symlinked {label}: {current}")


def _default_project_root() -> Path:
    configured_root = os.environ.get(PROJECT_ROOT_ENV)
    if configured_root:
        project_root = Path(configured_root).expanduser().resolve()
        if not _is_agent_smith_root(project_root):
            raise RuntimeError(
                f"{PROJECT_ROOT_ENV} must point to an Agent-Smith root with runtime assets"
            )
        return project_root

    source_root = Path(__file__).resolve().parent.parent
    if _is_agent_smith_root(source_root):
        return source_root

    # Stricter validation: check for Agent-Smith signature files
    # to avoid mistaking another project's agents/ directory
    working_dir = Path.cwd().resolve()
    for candidate in (working_dir, *working_dir.parents):
        if not (candidate / "agents").is_dir():
            continue

        if _is_agent_smith_root(candidate):
            return candidate

        # Log skipped candidates to make root-discovery mismatches diagnosable.
        logger.debug("Skipping %s: has agents/ but missing Agent-Smith markers", candidate)

    raise RuntimeError(
        "Unable to locate an Agent-Smith project root with runtime assets; "
        f"set {PROJECT_ROOT_ENV} to a root with runtime assets"
    )


def _ensure_private_dir(path: Path) -> None:
    _ensure_real_path(path, label="private runtime path")
    if path.exists():
        if not path.is_dir():
            raise NotADirectoryError(f"Private runtime path is not a directory: {path}")
        return
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    path.chmod(PRIVATE_DIR_MODE)


def _ensure_real_descendant(root: Path, path: Path) -> None:
    """Reject symlinks anywhere in a managed target path."""
    _ensure_real_path(root, label="managed path")
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
    """Remove a managed path without following a symlink at the final path."""
    # A stale leaf symlink is safe to unlink: unlink() removes the link itself
    # and cannot touch its target.  Its parents must still be real managed
    # directories, otherwise a path such as ``target/link/stale`` could escape
    # the managed tree.
    _ensure_real_descendant(root, path.parent)
    if path.is_symlink():
        path.unlink(missing_ok=True)
        return

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


def _file_metadata(path: Path) -> dict[str, int]:
    stat_result = path.stat()
    return {"mtime_ns": stat_result.st_mtime_ns, "size": stat_result.st_size}


def _matches_file_metadata(path: Path, metadata: object) -> bool:
    if not isinstance(metadata, dict):
        return False
    expected_mtime = metadata.get("mtime_ns")
    expected_size = metadata.get("size")
    if not isinstance(expected_mtime, int) or not isinstance(expected_size, int):
        return False
    actual = _file_metadata(path)
    return actual["mtime_ns"] == expected_mtime and actual["size"] == expected_size


def _manifest_entry_matches(
    source_file: Path, target_file: Path, entry: object
) -> bool:
    if not target_file.is_file() or not isinstance(entry, dict):
        return False
    source = entry.get("source")
    target = entry.get("target")
    if not isinstance(source, dict) or not isinstance(source.get("sha256"), str):
        return False
    return _matches_file_metadata(source_file, source) and _matches_file_metadata(
        target_file, target
    )


@dataclass(frozen=True)
class AppPaths:
    data_dir: Path
    project_root: Path

    @classmethod
    def defaults(cls) -> AppPaths:
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

        Incremental sync uses the manifest's size and nanosecond mtime metadata
        to skip unchanged files.  When metadata differs, SHA-256 verification
        decides whether the managed copy must be restored.
        """
        source = self.bundled_skills_dir
        if not source.is_dir():
            return

        target = self.builtin_skills_dir
        _ensure_private_dir(target.parent)
        _ensure_private_dir(target)
        manifest_path = target / ".manifest.json"
        _prepare_managed_file(target, manifest_path)
        previous_files: dict[str, object] = {}
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                manifest = None
            if isinstance(manifest, dict) and isinstance(manifest.get("files"), dict):
                previous_files = manifest["files"]

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
                file_key = str(rel_path)
                previous_entry = previous_files.get(file_key)
                if _manifest_entry_matches(source_file, target_file, previous_entry):
                    current_manifest["files"][file_key] = previous_entry
                    continue

                source_digest = _file_digest(source_file)
                if not target_file.is_file() or _file_digest(target_file) != source_digest:
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_file, target_file)
                    target_file.chmod(PRIVATE_FILE_MODE)

                # Record in new manifest
                current_manifest["files"][file_key] = {
                    "source": {**_file_metadata(source_file), "sha256": source_digest},
                    "target": _file_metadata(target_file),
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
            if (child.is_dir() or child.is_symlink()) and child.name not in shipped:
                _remove_managed_path(target, child)

        # Write new manifest
        manifest_path.write_text(
            json.dumps(current_manifest, indent=2), encoding="utf-8"
        )
        manifest_path.chmod(PRIVATE_FILE_MODE)
