from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from engine.skill.store import SkillStore


def test_skill_store_rejects_traversal_skill_name(tmp_path: Path):
    store = SkillStore(tmp_path / "skills")

    with pytest.raises(ValueError):
        asyncio.run(store.save_version("..", "escaped"))

    assert not (tmp_path / ".versions").exists()


def test_skill_store_rejects_symlinked_skill_directory(tmp_path: Path):
    skills = tmp_path / "skills"
    skills.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (skills / "linked").symlink_to(outside, target_is_directory=True)
    store = SkillStore(skills)

    with pytest.raises(ValueError, match="escapes skills root"):
        asyncio.run(store.save_version("linked", "escaped"))
