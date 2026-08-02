from __future__ import annotations

from pathlib import Path

from engine.skill.loader import SkillBody, SkillMeta, parse_skill_md
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


def test_agent_skill_refresh_replaces_stale_entries_and_restores_builtins(
    tmp_path: Path,
) -> None:
    builtin_dir = tmp_path / "builtin"
    agent_dir = tmp_path / "agent"
    _write_skill(builtin_dir, "shared", "---\nname: shared\n---\nBuiltin")
    shared_file = _write_skill(agent_dir, "shared", "---\nname: shared\n---\nAgent")
    _write_skill(agent_dir, "old-name", "---\nname: old-name\n---\nOld")

    registry = SkillRegistry()
    registry.load_builtin(builtin_dir)
    registry.load_agent_skills(agent_dir)
    assert registry.get("shared").content == "Agent"  # type: ignore[union-attr]
    assert registry.get("old-name") is not None

    shared_file.unlink()
    (agent_dir / "old-name" / "SKILL.md").unlink()
    _write_skill(agent_dir, "new-name", "---\nname: new-name\n---\nNew")
    registry.load_agent_skills(agent_dir)

    assert registry.get("shared").content == "Builtin"  # type: ignore[union-attr]
    assert registry.is_builtin("shared")
    assert registry.get("old-name") is None
    assert registry.get("new-name").content == "New"  # type: ignore[union-attr]


def test_agent_skill_registry_rejects_directory_frontmatter_name_mismatch(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    _write_skill(
        skills_dir,
        "directory-name",
        "---\nname: declared-name\n---\nBody",
    )

    registry = SkillRegistry()
    registry.load_agent_skills(skills_dir)

    assert registry.get("directory-name") is None
    assert registry.get("declared-name") is None
    assert registry.get_agent_skill_dir("directory-name") is None
    assert registry.get_agent_skill_dir("declared-name") is None


def test_agent_skill_registry_does_not_follow_symlinks_outside_catalog(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    outside_dir = tmp_path / "outside-skill"
    _write_skill(
        outside_dir.parent,
        outside_dir.name,
        "---\nname: linked-directory\n---\nOutside directory",
    )
    (skills_dir / "linked-directory").symlink_to(
        outside_dir,
        target_is_directory=True,
    )

    linked_file_dir = skills_dir / "linked-file"
    linked_file_dir.mkdir()
    outside_file = tmp_path / "outside.md"
    outside_file.write_text(
        "---\nname: linked-file\n---\nOutside file",
        encoding="utf-8",
    )
    (linked_file_dir / "SKILL.md").symlink_to(outside_file)

    registry = SkillRegistry()
    registry.load_agent_skills(skills_dir)

    assert registry.get("linked-directory") is None
    assert registry.get("linked-file") is None
    assert registry.get_agent_skill_dir("linked-directory") is None
    assert registry.get_agent_skill_dir("linked-file") is None


def test_agent_skill_registry_does_not_follow_symlinked_catalog_root(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-skills"
    _write_skill(
        outside,
        "external",
        "---\nname: external\n---\nOutside catalog",
    )
    linked_catalog = tmp_path / "skills"
    linked_catalog.symlink_to(outside, target_is_directory=True)

    registry = SkillRegistry()
    registry.load_agent_skills(linked_catalog)

    assert registry.get("external") is None
    assert registry.get_agent_skill_dir("external") is None


def test_builtin_registry_does_not_follow_symlinks_outside_catalog(
    tmp_path: Path,
) -> None:
    builtin_dir = tmp_path / "builtin"
    builtin_dir.mkdir()
    outside = tmp_path / "outside"
    _write_skill(
        outside,
        "linked-directory",
        "---\nname: linked-directory\n---\nOutside directory",
    )
    (builtin_dir / "linked-directory").symlink_to(
        outside / "linked-directory",
        target_is_directory=True,
    )

    linked_file_dir = builtin_dir / "linked-file"
    linked_file_dir.mkdir()
    outside_file = tmp_path / "outside.md"
    outside_file.write_text(
        "---\nname: linked-file\n---\nOutside file",
        encoding="utf-8",
    )
    (linked_file_dir / "SKILL.md").symlink_to(outside_file)

    registry = SkillRegistry()
    registry.load_builtin(builtin_dir)

    assert registry.get("linked-directory") is None
    assert registry.get("linked-file") is None


def test_builtin_registry_does_not_follow_symlinked_catalog_root(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-skills"
    _write_skill(
        outside,
        "external",
        "---\nname: external\n---\nOutside catalog",
    )
    linked_catalog = tmp_path / "builtin"
    linked_catalog.symlink_to(outside, target_is_directory=True)

    registry = SkillRegistry()
    registry.load_builtin(linked_catalog)

    assert registry.get("external") is None


def test_agent_skill_refresh_resets_stale_allowlist_so_new_skills_load(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "existing", "---\nname: existing\n---\nOK")
    registry = SkillRegistry()
    registry.load_agent_skills(skills_dir)
    registry.restrict_to({"existing"})
    assert registry.get("existing") is not None

    # A skill created mid-request becomes visible after the refresh.
    _write_skill(skills_dir, "new-skill", "---\nname: new-skill\n---\nNew")
    registry.load_agent_skills(skills_dir)
    assert registry.get("new-skill") is not None
    assert registry.get("existing") is not None

    # Re-applying the restriction from the full catalog still narrows.
    registry.restrict_to({"existing"})
    assert registry.get("new-skill") is None
    assert registry.get("existing") is not None


def test_reapplied_disabled_restriction_after_refresh_keeps_new_skill(
    tmp_path: Path,
) -> None:
    """Mirror bind_skill_manage_tool's post-mutation reload (disabled_skills).

    The allowlist must be rebuilt from the full catalog, not intersected with
    the already-filtered set, or a freshly created skill stays invisible for
    the rest of the request.
    """
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "existing", "---\nname: existing\n---\nOK")
    registry = SkillRegistry()
    registry.load_agent_skills(skills_dir)

    disabled = {"existing"}
    registry.restrict_to([
        summary["name"]
        for summary in registry.list_summaries()
        if summary["name"] not in disabled
    ])
    assert registry.get("existing") is None

    # skill_manage creates a new skill, then the wrapper reloads + re-applies.
    _write_skill(skills_dir, "new-skill", "---\nname: new-skill\n---\nNew")
    registry.load_agent_skills(skills_dir)
    registry.restrict_to([
        summary["name"]
        for summary in registry.list_summaries()
        if summary["name"] not in disabled
    ])
    assert registry.get("new-skill") is not None
    assert registry.get("existing") is None


def test_reapplied_enabled_restriction_after_refresh_includes_new_skill(
    tmp_path: Path,
) -> None:
    """Mirror bind_skill_manage_tool's post-mutation reload (enabled_skills)."""
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "existing", "---\nname: existing\n---\nOK")
    registry = SkillRegistry()
    registry.load_agent_skills(skills_dir)
    registry.restrict_to({"existing"})

    _write_skill(skills_dir, "new-skill", "---\nname: new-skill\n---\nNew")
    registry.load_agent_skills(skills_dir)
    registry.restrict_to({"existing", "new-skill"})

    assert registry.get("new-skill") is not None
    assert registry.get("existing") is not None


def test_skill_conversation_caps_oversized_skill_body() -> None:
    from engine.skill.executor import _skill_conversation

    huge_body = "z" * 200_000
    skill = SkillBody(meta=SkillMeta(name="big"), content=huge_body)
    conversation = _skill_conversation(
        skill,
        [
            {"role": "system", "content": "IDENTITY=smith"},
            {"role": "user", "content": "go"},
        ],
        {},
    )
    skill_layer = next(
        message["content"]
        for message in conversation
        if "# Skill: big" in str(message.get("content"))
    )
    assert "[... truncated ...]" in skill_layer
    assert len(skill_layer) < len(huge_body)

    # A normal-size skill body passes through unmodified.
    small = SkillBody(meta=SkillMeta(name="small"), content="Use tools.")
    conversation = _skill_conversation(small, [{"role": "user", "content": "go"}], {})
    assert "Use tools." in str(conversation[0]["content"])
