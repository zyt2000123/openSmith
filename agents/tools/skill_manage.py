from __future__ import annotations

"""Skill management tool provider — list, read, create, edit, patch, and version skills.

Built-in skills (under agents/skills/) are READ-ONLY.
Only Smith-installed skills (under ~/.agent-smith/agent/skills/) can be modified.
"""
# 内置技能不可变；写操作只能落在 Smith 的运行时安装目录，且保留版本回滚。

import asyncio
import os
import re
from pathlib import Path

import yaml

TOOL_META = {
    "name": "skill_manage",
    "hidden": False,
    "description": (
        "Manage agent skills: list, get, create, edit, patch, versions, rollback. "
        "Built-in skills are read-only; only agent-installed skills can be modified."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "get", "create", "edit", "patch", "versions", "rollback"],
                "description": "The skill management operation to perform",
            },
            "skill_name": {
                "type": "string",
                "description": "Skill name (required for get/create/edit/patch/versions/rollback)",
            },
            "content": {
                "type": "string",
                "description": "Full SKILL.md content (required for create/edit)",
            },
            "section": {
                "type": "string",
                "description": "Section heading to patch, e.g. '## Process' (required for patch)",
            },
            "section_content": {
                "type": "string",
                "description": "New content for the section (required for patch)",
            },
            "version_id": {
                "type": "string",
                "description": "Version id (required for rollback)",
            },
        },
        "required": ["action"],
    },
    "is_write_tool": True,
    "permission_level": "write",
    "approval_policy": "policy",
    "read_actions": ["list", "get", "versions"],
    "side_effect": "write",
    "concurrency": "serial",
    "execution_environment": "host",
}

# Builtin skills directory — resolved relative to this file
_BUILTIN_SKILLS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "skills")
MAX_SKILL_BYTES = 8 * 1024 * 1024  # 8MB per SKILL.md, mirroring write_file's cap


def _agent_skills_dir(agent_skills_dir: str | Path | None) -> Path:
    if agent_skills_dir is None:
        raise RuntimeError("agent skill storage was not provided by the runtime")
    path = Path(agent_skills_dir)
    if path.is_symlink():
        raise RuntimeError("agent skill storage must not be a symlink")
    return path


def _is_builtin(skill_name: str) -> bool:
    safe = os.path.basename(skill_name)
    return os.path.isfile(os.path.join(_BUILTIN_SKILLS_DIR, safe, "SKILL.md"))


def _is_safe_skill_name(skill_name: str) -> bool:
    """Only accept a single path component; never silently normalize it."""
    safe = Path(skill_name).name
    return bool(skill_name) and safe == skill_name and safe not in {".", ".."}


def _agent_skill_dir(agent_skills_dir: Path, skill_name: str) -> Path:
    path = agent_skills_dir.resolve() / skill_name
    if path.is_symlink() or not path.resolve(strict=False).is_relative_to(agent_skills_dir.resolve()):
        raise ValueError("skill directory escapes agent skills root")
    return path


def _parse_frontmatter(raw: str) -> dict:
    """Extract YAML frontmatter from SKILL.md content."""
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            loaded = yaml.safe_load(parts[1])
            return loaded if isinstance(loaded, dict) else {}
    return {}


def _validate_skill_content_name(skill_name: str, raw: str) -> str | None:
    try:
        meta = _parse_frontmatter(raw)
    except yaml.YAMLError as exc:
        return f"invalid YAML frontmatter: {exc}"
    declared = meta.get("name")
    if declared is None:
        return None
    declared_name = str(declared)
    if declared_name != skill_name:
        return (
            f"frontmatter name '{declared_name}' must match "
            f"skill_name '{skill_name}'"
        )
    return None


def _list_all_skills(agent_skills_dir: Path) -> list[dict]:
    """List builtin + agent-installed skills with metadata."""
    skills: list[dict] = []

    # Builtin
    builtin_dir = Path(_BUILTIN_SKILLS_DIR)
    if builtin_dir.is_dir():
        for child in sorted(builtin_dir.iterdir()):
            sf = child / "SKILL.md"
            if sf.is_file():
                meta = _parse_frontmatter(sf.read_text(encoding="utf-8"))
                skills.append({
                    "name": meta.get("name", child.name),
                    "description": meta.get("description", ""),
                    "version": meta.get("version", "0.1.0"),
                    "source": "builtin",
                })

    # Agent-installed
    if agent_skills_dir.is_dir():
        for child in sorted(agent_skills_dir.iterdir()):
            sf = child / "SKILL.md"
            if not child.is_symlink() and not sf.is_symlink() and sf.is_file():
                meta = _parse_frontmatter(sf.read_text(encoding="utf-8"))
                skills.append({
                    "name": meta.get("name", child.name),
                    "description": meta.get("description", ""),
                    "version": meta.get("version", "0.1.0"),
                    "source": "agent",
                })

    return skills


def _get_skill_content(agent_skills_dir: Path, skill_name: str) -> tuple[str, str]:
    """Return (content, source) for a skill. Checks agent first, then builtin."""
    if not _is_safe_skill_name(skill_name):
        return "", ""

    # Agent-installed first
    try:
        agent_path = _agent_skill_dir(agent_skills_dir, skill_name) / "SKILL.md"
    except ValueError:
        return "", ""
    if not agent_path.is_symlink() and agent_path.is_file():
        return agent_path.read_text(encoding="utf-8"), "agent"

    # Builtin
    safe = skill_name
    builtin_path = Path(_BUILTIN_SKILLS_DIR) / safe / "SKILL.md"
    if builtin_path.is_file():
        return builtin_path.read_text(encoding="utf-8"), "builtin"

    return "", ""


def _fence_marker(line: str) -> tuple[str, int] | None:
    """Return ``(char, length)`` when the line opens or closes a fenced block."""
    stripped = line.strip()
    for char in ("`", "~"):
        if stripped.startswith(char * 3):
            return char, len(stripped) - len(stripped.lstrip(char))
    return None


def _track_fence(current: tuple[str, int] | None, line: str) -> tuple[str, int] | None:
    """Advance fenced-block state across one line."""
    marker = _fence_marker(line)
    if marker is None:
        return current
    if current is None:
        return marker
    # A fence closes only on the same character, at least as long as the opener.
    if marker[0] == current[0] and marker[1] >= current[1]:
        return None
    return current


def _patch_section(raw: str, section_heading: str, new_content: str) -> str:
    """Replace a markdown section's content, preserving everything else.

    *section_heading* should be a heading like '## Process' or '### Step 1'.

    Headings count only outside fenced code blocks.  A SKILL.md that documents
    its own format naturally contains example blocks holding heading-like lines;
    treating those as real boundaries ended the replace scope early and left
    duplicate headings, an unclosed fence, and un-replaced old content behind —
    while still reporting success.
    """
    # Determine heading level from the target
    match = re.match(r"^(#{1,6})\s+", section_heading)
    if not match:
        raise ValueError(f"Invalid section heading: {section_heading}")
    level = len(match.group(1))
    target = section_heading.strip()

    lines = raw.split("\n")
    result: list[str] = []
    i = 0
    patched = False
    fence: tuple[str, int] | None = None

    while i < len(lines):
        line = lines[i]
        was_inside_fence = fence is not None
        fence = _track_fence(fence, line)
        # Check if this line matches the target section heading
        if not patched and not was_inside_fence and fence is None and line.strip() == target:
            # Emit the heading
            result.append(line)
            i += 1
            # Skip old content until we hit a heading of same or higher level, or EOF
            skip_fence: tuple[str, int] | None = None
            while i < len(lines):
                next_line = lines[i]
                inside = skip_fence is not None
                skip_fence = _track_fence(skip_fence, next_line)
                if not inside and skip_fence is None:
                    heading_match = re.match(r"^(#{1,6})\s+", next_line)
                    if heading_match and len(heading_match.group(1)) <= level:
                        break
                i += 1
            # Insert new content
            result.append(new_content.rstrip())
            result.append("")
            patched = True
        else:
            result.append(line)
            i += 1

    if not patched:
        raise ValueError(f"Section '{section_heading}' not found in SKILL.md")

    return "\n".join(result)


async def execute(
    *,
    action: str,
    skill_name: str | None = None,
    content: str | None = None,
    section: str | None = None,
    section_content: str | None = None,
    version_id: str | None = None,
    agent_skills_dir: str | Path | None = None,
    skill_store: object | None = None,
) -> str:
    try:
        resolved_skills_dir = _agent_skills_dir(agent_skills_dir)
    except RuntimeError as exc:
        return f"Error: {exc}"
    if skill_store is None:
        return "Error: skill version store was not provided by the runtime"
    store = skill_store

    if skill_name is not None and not _is_safe_skill_name(skill_name):
        return "Error: skill_name must be a single non-relative path component"

    # ------------------------------------------------------------------
    # list
    # ------------------------------------------------------------------
    if action == "list":
        skills = await asyncio.to_thread(_list_all_skills, resolved_skills_dir)
        if not skills:
            return "No skills found."
        lines = [f"Found {len(skills)} skill(s):\n"]
        for s in skills:
            tag = "[builtin]" if s["source"] == "builtin" else "[agent]"
            lines.append(f"- {tag} {s['name']} v{s['version']}: {s['description']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # get
    # ------------------------------------------------------------------
    if action == "get":
        if not skill_name:
            return "Error: 'skill_name' is required for get action"
        raw, source = await asyncio.to_thread(
            _get_skill_content,
            resolved_skills_dir,
            skill_name,
        )
        if not raw:
            return f"Error: skill '{skill_name}' not found"
        return f"# Skill: {skill_name} [{source}]\n\n{raw}"

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------
    if action == "create":
        if not skill_name:
            return "Error: 'skill_name' is required for create action"
        if not content:
            return "Error: 'content' is required for create action"
        if _is_builtin(skill_name):
            return f"Error: '{skill_name}' is a built-in skill name. Choose a different name."
        if len(content.encode("utf-8")) > MAX_SKILL_BYTES:
            return (
                f"Error: content exceeds the {MAX_SKILL_BYTES // (1024 * 1024)} MB "
                "per-skill size limit"
            )
        content_error = _validate_skill_content_name(skill_name, content)
        if content_error:
            return f"Error: {content_error}"

        try:
            skill_dir = _agent_skill_dir(resolved_skills_dir, skill_name)
        except ValueError:
            return "Error: skill directory must not escape agent skills storage"
        skill_file = skill_dir / "SKILL.md"
        if skill_file.is_symlink():
            return "Error: skill file must not be a symlink"
        if skill_file.is_file():
            return f"Error: skill '{skill_name}' already exists. Use 'edit' to modify it."

        await asyncio.to_thread(skill_dir.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(skill_file.write_text, content, encoding="utf-8")
        return f"OK: created skill '{skill_name}' at {skill_file}"

    # ------------------------------------------------------------------
    # edit (full rewrite)
    # ------------------------------------------------------------------
    if action == "edit":
        if not skill_name:
            return "Error: 'skill_name' is required for edit action"
        if not content:
            return "Error: 'content' is required for edit action"
        if _is_builtin(skill_name):
            return "Error: built-in skills are read-only. Cannot edit."
        if len(content.encode("utf-8")) > MAX_SKILL_BYTES:
            return (
                f"Error: content exceeds the {MAX_SKILL_BYTES // (1024 * 1024)} MB "
                "per-skill size limit"
            )
        content_error = _validate_skill_content_name(skill_name, content)
        if content_error:
            return f"Error: {content_error}"

        try:
            skill_file = _agent_skill_dir(resolved_skills_dir, skill_name) / "SKILL.md"
        except ValueError:
            return "Error: skill directory must not escape agent skills storage"
        if skill_file.is_symlink():
            return "Error: skill file must not be a symlink"
        if not skill_file.is_file():
            return f"Error: skill '{skill_name}' not found in agent skills. Use 'create' first."

        # Auto-save version before editing
        old_content = await asyncio.to_thread(
            skill_file.read_text,
            encoding="utf-8",
        )
        vid = await store.save_version(skill_name, old_content)

        await asyncio.to_thread(skill_file.write_text, content, encoding="utf-8")
        return f"OK: edited skill '{skill_name}' (previous version saved as {vid})"

    # ------------------------------------------------------------------
    # patch (section-level edit)
    # ------------------------------------------------------------------
    if action == "patch":
        if not skill_name:
            return "Error: 'skill_name' is required for patch action"
        if not section:
            return "Error: 'section' is required for patch action"
        if section_content is None:
            return "Error: 'section_content' is required for patch action"
        if _is_builtin(skill_name):
            return "Error: built-in skills are read-only. Cannot patch."

        try:
            skill_file = _agent_skill_dir(resolved_skills_dir, skill_name) / "SKILL.md"
        except ValueError:
            return "Error: skill directory must not escape agent skills storage"
        if skill_file.is_symlink():
            return "Error: skill file must not be a symlink"
        if not skill_file.is_file():
            return f"Error: skill '{skill_name}' not found in agent skills"

        old_content = await asyncio.to_thread(
            skill_file.read_text,
            encoding="utf-8",
        )

        try:
            new_content = _patch_section(old_content, section, section_content)
        except ValueError as e:
            return f"Error: {e}"

        # ``create`` and ``edit`` both cap the content they write; ``patch`` did
        # not, so repeated section patches were an unbounded write path to a
        # file that is loaded into every later prompt.  Check the *result*, not
        # the fragment: a small fragment appended to an already-large file is
        # what actually crosses the limit.
        if len(new_content.encode("utf-8")) > MAX_SKILL_BYTES:
            return (
                f"Error: patched content exceeds the {MAX_SKILL_BYTES // (1024 * 1024)} MB "
                "per-skill size limit"
            )

        # Save the version only once the patch is known to apply.  Saving first
        # meant a run of failed patches — a model guessing at section names is
        # normal — consumed the bounded history and evicted the genuine pre-edit
        # content, defeating the one thing rollback exists for.
        vid = await store.save_version(skill_name, old_content)

        await asyncio.to_thread(
            skill_file.write_text,
            new_content,
            encoding="utf-8",
        )
        return f"OK: patched section '{section}' in skill '{skill_name}' (previous version saved as {vid})"

    # ------------------------------------------------------------------
    # versions
    # ------------------------------------------------------------------
    if action == "versions":
        if not skill_name:
            return "Error: 'skill_name' is required for versions action"

        versions = await store.list_versions(skill_name)
        if not versions:
            return f"No versions found for skill '{skill_name}'"

        lines = [f"Versions for '{skill_name}' ({len(versions)}):"]
        for v in versions:
            lines.append(f"- {v['version_id']}  ({v['size']} bytes)")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # rollback
    # ------------------------------------------------------------------
    if action == "rollback":
        if not skill_name:
            return "Error: 'skill_name' is required for rollback action"
        if not version_id:
            return "Error: 'version_id' is required for rollback action"
        if _is_builtin(skill_name):
            return "Error: built-in skills are read-only. Cannot rollback."

        ok = await store.rollback(skill_name, version_id)
        if not ok:
            return f"Error: version '{version_id}' not found for skill '{skill_name}'"
        return f"OK: rolled back skill '{skill_name}' to version '{version_id}'"

    return f"Error: unknown action '{action}'. Use: list, get, create, edit, patch, versions, rollback"
