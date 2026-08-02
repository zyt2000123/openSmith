from __future__ import annotations

import os
from pathlib import Path

from fastapi import HTTPException

from common import config as common_config
from engine.safety.tool_guard import ToolGuard
from engine.tool.interface import ToolCall, ToolDefinition

from ..schemas.project_instruction import ProjectInstructionOut


PROJECT_INSTRUCTION_TEMPLATE = """# Agent-Smith Project Instructions

Use this file for instructions that should apply whenever Agent-Smith works in this repository.

## Project overview

- Describe the product, important directories, and architectural boundaries.

## Development workflow

- Record the standard build, test, lint, and verification commands.
- Note coding conventions and any required review checks.

## Safety and collaboration

- List project-specific constraints, protected files, and release expectations.
"""


class ProjectInstructionService:
    """Create the one project instruction file through the same write guard as tools."""

    async def initialize(self, working_dir: str | Path) -> ProjectInstructionOut:
        project_root = self._project_root(working_dir)
        smith_dir = project_root / ".smith"
        target = smith_dir / "SMITH.md"

        self._ensure_safe_target(smith_dir, target)
        if target.is_file():
            return ProjectInstructionOut(path=str(target), created=False)
        if target.exists():
            raise HTTPException(409, "Project instruction path is unsafe")

        guard = ToolGuard(
            common_config.PATHS.safety_rules_path,
            allowed_dirs=[],
            tool_registry={
                "write_file": ToolDefinition(
                    name="write_file",
                    description="",
                    path_args=("path",),
                    is_write_tool=True,
                    permission_level="write",
                    approval_policy="policy",
                    side_effect="write",
                ),
            },
        )
        whitelisted_target = guard.allow_project_instruction_path(project_root)
        if whitelisted_target != target:
            raise HTTPException(409, "Project instruction path is unsafe")
        decision = guard.check(
            ToolCall(
                id="project-init",
                name="write_file",
                arguments={"path": str(target), "content": PROJECT_INSTRUCTION_TEMPLATE},
            )
        )
        if not decision.allowed:
            raise HTTPException(403, "Project instructions cannot be initialized at this path")

        try:
            # exist_ok: a .smith/ left by other tooling must not be mistaken for
            # an existing SMITH.md — the FileExistsError below belongs to O_EXCL.
            smith_dir.mkdir(mode=0o755, exist_ok=True)
            self._ensure_safe_target(smith_dir, target)
            no_follow = getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(PROJECT_INSTRUCTION_TEMPLATE)
        except FileExistsError:
            return ProjectInstructionOut(path=str(target), created=False)
        except OSError as exc:
            raise HTTPException(500, "Unable to create project instructions") from exc

        return ProjectInstructionOut(path=str(target), created=True)

    @staticmethod
    def _project_root(working_dir: str | Path) -> Path:
        try:
            start = Path(working_dir).expanduser().resolve(strict=True)
        except OSError as exc:
            raise HTTPException(422, "Working directory is unavailable") from exc
        if not start.is_dir():
            raise HTTPException(422, "Working directory must be a directory")

        for candidate in (start, *start.parents):
            if (candidate / ".git").exists():
                return candidate
        return start

    @staticmethod
    def _ensure_safe_target(smith_dir: Path, target: Path) -> None:
        if smith_dir.is_symlink() or target.is_symlink():
            raise HTTPException(409, "Project instruction path is unsafe")
        if smith_dir.exists() and not smith_dir.is_dir():
            raise HTTPException(409, "Project instruction path is unsafe")
