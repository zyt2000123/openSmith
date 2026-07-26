from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from app.services.project_instruction_service import (
    PROJECT_INSTRUCTION_TEMPLATE,
    ProjectInstructionService,
)


@pytest.mark.asyncio
async def test_initialize_creates_the_whitelisted_project_instruction_at_git_root(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    nested_dir = project_root / "packages" / "shell"
    (project_root / ".git").mkdir(parents=True)
    nested_dir.mkdir(parents=True)

    result = await ProjectInstructionService().initialize(nested_dir)

    target = project_root / ".smith" / "SMITH.md"
    assert result.created is True
    assert result.path == str(target)
    assert target.read_text(encoding="utf-8") == PROJECT_INSTRUCTION_TEMPLATE


@pytest.mark.asyncio
async def test_initialize_never_overwrites_an_existing_instruction(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    target = project_root / ".smith" / "SMITH.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Existing instructions\n", encoding="utf-8")

    result = await ProjectInstructionService().initialize(project_root)

    assert result.created is False
    assert target.read_text(encoding="utf-8") == "# Existing instructions\n"


@pytest.mark.asyncio
async def test_initialize_writes_into_a_smith_directory_left_by_other_tooling(tmp_path: Path) -> None:
    """An existing .smith/ is not evidence that SMITH.md exists.

    mkdir() without exist_ok raised FileExistsError for the directory, which the
    O_EXCL handler swallowed as "already initialized" — reporting created=False
    while never writing the file.
    """
    project_root = tmp_path / "project"
    (project_root / ".git").mkdir(parents=True)
    (project_root / ".smith").mkdir()

    result = await ProjectInstructionService().initialize(project_root)

    target = project_root / ".smith" / "SMITH.md"
    assert result.created is True
    assert target.read_text(encoding="utf-8") == PROJECT_INSTRUCTION_TEMPLATE


@pytest.mark.asyncio
async def test_initialize_rejects_a_symlinked_instruction_directory(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    external_dir = tmp_path / "external"
    project_root.mkdir()
    external_dir.mkdir()
    (project_root / ".smith").symlink_to(external_dir, target_is_directory=True)

    with pytest.raises(HTTPException, match="unsafe"):
        await ProjectInstructionService().initialize(project_root)

    assert not (external_dir / "SMITH.md").exists()
