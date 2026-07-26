from __future__ import annotations

from pathlib import Path

from engine.skill.loader import parse_skill_md
from engine.skill.registry import SkillRegistry


def _write_skill(root: Path, dirname: str, text: str) -> Path:
    skill_dir = root / dirname
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(text, encoding="utf-8")
    return skill_file


def test_parse_skill_md_with_valid_frontmatter(tmp_path: Path):
    f = _write_skill(
        tmp_path, "review",
        "---\nname: review\ndescription: code review\nversion: 0.2\n---\nBody here",
    )
    skill = parse_skill_md(f)
    assert skill.meta.name == "review"
    assert skill.meta.description == "code review"
    assert skill.meta.version == "0.2"  # non-str YAML scalar coerced to str
    assert skill.content == "Body here"


def test_parse_skill_md_tolerates_non_mapping_frontmatter(tmp_path: Path):
    # Scalar frontmatter previously crashed with AttributeError on .get
    f = _write_skill(tmp_path, "scalar", "---\njust a string\n---\nBody")
    skill = parse_skill_md(f)
    assert skill.meta.name == "scalar"  # falls back to directory name
    assert skill.content == "Body"


def test_parse_skill_md_tolerates_invalid_yaml_frontmatter(tmp_path: Path):
    f = _write_skill(tmp_path, "broken", "---\nname: [unclosed\n---\nBody")
    skill = parse_skill_md(f)
    assert skill.meta.name == "broken"
    assert skill.content == "Body"


def test_registry_skips_unreadable_skill_and_loads_the_rest(tmp_path: Path):
    _write_skill(tmp_path, "good", "---\nname: good\n---\nOK")
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    (bad_dir / "SKILL.md").write_bytes(b"\xff\xfe invalid utf-8 \xff")

    registry = SkillRegistry()
    registry.load_builtin(tmp_path)

    assert registry.get("good") is not None
    assert registry.get("bad") is None


def test_get_agent_skill_dir_rejects_path_traversal(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "installed", "---\nname: installed\n---\nOK")
    (tmp_path / "outside").mkdir()

    registry = SkillRegistry()
    registry.load_agent_skills(skills_dir)

    assert registry.get_agent_skill_dir("installed") == skills_dir / "installed"
    assert registry.get_agent_skill_dir("../outside") is None


def test_agent_skill_override_is_reported_as_agent_skill(tmp_path: Path):
    builtin_dir = tmp_path / "builtin"
    agent_dir = tmp_path / "agent"
    _write_skill(builtin_dir, "shared", "---\nname: shared\n---\nBuiltin")
    _write_skill(agent_dir, "shared", "---\nname: shared\n---\nAgent")

    registry = SkillRegistry()
    registry.load_builtin(builtin_dir)
    registry.load_agent_skills(agent_dir)

    assert not registry.is_builtin("shared")
    assert registry.get_agent_skill_dir("shared") == agent_dir / "shared"
    assert registry.list_summaries()[0]["source"] == "agent"
