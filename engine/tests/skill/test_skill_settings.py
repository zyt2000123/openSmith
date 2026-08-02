from pathlib import Path

import pytest

from engine.skill.settings import SkillSettingsError, disabled_skill_names, set_skill_enabled


def test_all_skills_are_enabled_until_one_is_disabled(tmp_path: Path) -> None:
    assert disabled_skill_names(tmp_path) == set()

    assert set_skill_enabled(tmp_path, "research", enabled=False) == {"research"}
    assert disabled_skill_names(tmp_path) == {"research"}

    assert set_skill_enabled(tmp_path, "research", enabled=True) == set()
    assert disabled_skill_names(tmp_path) == set()


def test_set_skill_enabled_strips_whitespace_so_entries_remain_strippable(tmp_path: Path) -> None:
    """A whitespace-padded name must be normalized before persisting; otherwise
    it creates a dead entry that reads back stripped but can never be unset."""
    assert set_skill_enabled(tmp_path, "  research  ", enabled=False) == {"research"}
    assert disabled_skill_names(tmp_path) == {"research"}

    assert set_skill_enabled(tmp_path, " research ", enabled=True) == set()
    assert disabled_skill_names(tmp_path) == set()


def test_invalid_skill_settings_fail_loudly(tmp_path: Path) -> None:
    settings = tmp_path / "skills.yaml"
    settings.write_text("disabled: research\n", encoding="utf-8")

    with pytest.raises(SkillSettingsError, match="disabled"):
        disabled_skill_names(tmp_path)
