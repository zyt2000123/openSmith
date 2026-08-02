from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class SkillMeta:
    name: str
    description: str = ""
    version: str = "0.1.0"
    argument_hint: str = ""


@dataclass
class SkillBody:
    meta: SkillMeta
    content: str  # markdown body after frontmatter


def parse_skill_md(path: Path) -> SkillBody:
    """Parse a SKILL.md file with YAML frontmatter + markdown body.

    Frontmatter is delimited by ``---`` fences on their own lines.  Line-based
    parsing (rather than ``raw.split("---", 2)``) is deliberate:
    - a ``---`` line inside a YAML block scalar (a multi-line description) is
      indented and never mistaken for the closing fence;
    - ``utf-8-sig`` strips a leading BOM, which would otherwise defeat the
      ``startswith("---")`` check and dump the whole file into the body.
    """
    raw = path.read_text(encoding="utf-8-sig")

    meta_dict: dict = {}
    body = raw

    if raw.startswith("---"):
        lines = raw.splitlines()
        closing = None
        for index, line in enumerate(lines[1:], start=1):
            if line == "---":
                closing = index
                break
        if closing is not None:
            try:
                loaded = yaml.safe_load("\n".join(lines[1:closing]))
            except yaml.YAMLError:
                loaded = None
            # Tolerate malformed frontmatter (invalid YAML or a non-mapping
            # root): strip the block but fall back to default metadata.
            meta_dict = loaded if isinstance(loaded, dict) else {}
            body = "\n".join(lines[closing + 1:]).strip()

    name = meta_dict.get("name")
    meta = SkillMeta(
        name=str(name) if name else path.parent.name,
        description=str(meta_dict.get("description") or ""),
        version=str(meta_dict.get("version") or "0.1.0"),
        argument_hint=str(meta_dict.get("argument_hint") or ""),
    )
    return SkillBody(meta=meta, content=body)
