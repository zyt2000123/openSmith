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


def test_skill_store_rejects_symlinked_skills_root(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    skills = tmp_path / "skills"
    skills.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="skills root must not be a symlink"):
        SkillStore(skills)


def test_skill_store_rollback_to_oldest_at_max_versions(tmp_path: Path):
    """Rolling back to the oldest snapshot must not delete it before restore."""
    store = SkillStore(tmp_path / "skills")
    name = "demo"
    version_ids = [
        asyncio.run(store.save_version(name, f"v{i}")) for i in range(10)
    ]
    skill_file = tmp_path / "skills" / name / "SKILL.md"
    skill_file.write_text("current", encoding="utf-8")

    oldest_id = version_ids[0]
    ok = asyncio.run(store.rollback(name, oldest_id))

    assert ok is True
    assert skill_file.read_text(encoding="utf-8") == "v0"


def test_skill_store_repeated_rollback_does_not_grow_version_set(tmp_path: Path):
    """Rolling back to a snapshot equal to current must not add a new version."""
    store = SkillStore(tmp_path / "skills")
    name = "demo"
    v0_id = asyncio.run(store.save_version(name, "v0"))
    asyncio.run(store.save_version(name, "v1"))
    skill_file = tmp_path / "skills" / name / "SKILL.md"
    skill_file.write_text("v1", encoding="utf-8")

    assert asyncio.run(store.rollback(name, v0_id)) is True
    assert skill_file.read_text(encoding="utf-8") == "v0"

    count_after_first = len(asyncio.run(store.list_versions(name)))
    assert asyncio.run(store.rollback(name, v0_id)) is True
    count_after_second = len(asyncio.run(store.list_versions(name)))

    assert count_after_second == count_after_first
