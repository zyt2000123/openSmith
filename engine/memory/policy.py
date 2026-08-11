"""Canonical structured-memory policy loading and validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml

MemoryViewName = Literal["context", "durable"]

# These machine-readable allowlists are the write-time counterpart to the
# Markdown policy.  They reject task-plan entries even when a caller labels
# them as evidence.
MANUAL_MEMORY_KINDS = frozenset({
    "preference",
    "correction",
    "decision",
    "remember",
    "forget",
    "verified_fact",
    "procedure",
    "pitfall",
})
MANUAL_EVIDENCE_TYPES = frozenset({
    "user_explicit",
    "tool_result",
    "test_result",
    "source_document",
})


class MemoryPolicyError(ValueError):
    """The canonical policy or a rendered memory view is invalid."""


@dataclass(frozen=True)
class MemoryViewPolicy:
    """Machine-readable contract for one rendered Markdown view."""

    name: MemoryViewName
    path: Path
    title: str
    scope: str
    load: str
    max_chars: int
    sections: tuple[str, ...]


@dataclass(frozen=True)
class MemoryPolicy:
    """The single policy shared by compiler, reviewer, Dream, and writer."""

    version: int
    views: dict[MemoryViewName, MemoryViewPolicy]
    markdown: str

    def view(self, name: MemoryViewName) -> MemoryViewPolicy:
        try:
            return self.views[name]
        except KeyError as exc:
            raise MemoryPolicyError(f"unknown memory view: {name}") from exc

    def instructions_for(
        self,
        name: MemoryViewName,
        *,
        role: Literal["compiler", "reviewer", "dream"],
    ) -> str:
        """Return only the shared and target-specific policy sections."""
        sections = _split_policy_sections(self.markdown)
        prefixes = ["1.", "2.", "3.", _VIEW_SECTION_PREFIX[name]]
        if role == "compiler":
            prefixes.append("6.")
        elif role == "reviewer":
            prefixes.append("7.")
        elif role == "dream":
            prefixes.extend(("7.", "8."))
        else:  # pragma: no cover - Literal protects normal callers
            raise MemoryPolicyError(f"unknown policy role: {role}")

        selected = ["# Smith Memory Policy"]
        for prefix in prefixes:
            section = next(
                (value for heading, value in sections.items() if heading.startswith(prefix)),
                None,
            )
            if section is None:
                raise MemoryPolicyError(f"policy section {prefix} is missing")
            selected.append(section)
        return "\n\n".join(selected).strip() + "\n"


_VIEW_SECTION_PREFIX: dict[MemoryViewName, str] = {
    "context": "4.",
    "durable": "5.",
}


def load_memory_policy(path: Path | None = None) -> MemoryPolicy:
    """Load and validate the packaged ``MEMORY_POLICY.md`` resource."""
    if path is None:
        text = resources.files("engine.memory").joinpath("MEMORY_POLICY.md").read_text(
            encoding="utf-8"
        )
    else:
        text = path.read_text(encoding="utf-8")

    metadata, markdown = _parse_frontmatter(text)
    version = metadata.get("policy_version")
    raw_views = metadata.get("views")
    if not isinstance(version, int) or version < 1:
        raise MemoryPolicyError("policy_version must be a positive integer")
    if not isinstance(raw_views, dict):
        raise MemoryPolicyError("views must be a mapping")

    expected = {"context", "durable"}
    if set(raw_views) != expected:
        raise MemoryPolicyError("policy must define context and durable views")

    views: dict[MemoryViewName, MemoryViewPolicy] = {}
    for raw_name, raw in raw_views.items():
        if not isinstance(raw, dict):
            raise MemoryPolicyError(f"view {raw_name} must be a mapping")
        name: MemoryViewName = raw_name
        relative_path = Path(str(raw.get("path", "")))
        if not relative_path.parts or relative_path.is_absolute() or ".." in relative_path.parts:
            raise MemoryPolicyError(f"view {name} path must be relative to agent_dir")
        sections = raw.get("sections")
        if not isinstance(sections, list) or not sections or not all(
            isinstance(section, str) and section.strip() for section in sections
        ):
            raise MemoryPolicyError(f"view {name} sections must be a non-empty string list")
        max_chars = raw.get("max_chars")
        if not isinstance(max_chars, int) or max_chars <= 0:
            raise MemoryPolicyError(f"view {name} max_chars must be positive")
        title = raw.get("title")
        if not isinstance(title, str) or not title.strip():
            raise MemoryPolicyError(f"view {name} title is required")
        scope = raw.get("scope")
        load = raw.get("load")
        if not isinstance(scope, str) or not scope.strip():
            raise MemoryPolicyError(f"view {name} scope is required")
        if not isinstance(load, str) or not load.strip():
            raise MemoryPolicyError(f"view {name} load strategy is required")
        views[name] = MemoryViewPolicy(
            name=name,
            path=relative_path,
            title=title.strip(),
            scope=scope.strip(),
            load=load.strip(),
            max_chars=max_chars,
            sections=tuple(section.strip() for section in sections),
        )

    policy = MemoryPolicy(version=version, views=views, markdown=markdown.strip())
    for name in views:
        policy.instructions_for(name, role="compiler")
        policy.instructions_for(name, role="reviewer")
    policy.instructions_for("durable", role="dream")
    return policy


def resolve_view_path(policy: MemoryPolicy, agent_dir: Path, name: MemoryViewName) -> Path:
    """Resolve a policy path relative to the runtime Agent profile directory."""
    root = agent_dir.resolve()
    target = agent_dir / policy.view(name).path
    if not target.resolve().is_relative_to(root):
        raise MemoryPolicyError(f"view {name} escaped agent_dir")
    return target


def validate_rendered_view(policy: MemoryPolicy, name: MemoryViewName, text: str) -> None:
    """Enforce the deterministic title, section, and budget contract."""
    spec = policy.view(name)
    if len(text) > spec.max_chars:
        raise MemoryPolicyError(
            f"{name} exceeded character budget ({len(text)} > {spec.max_chars})"
        )
    if "```" in text:
        raise MemoryPolicyError(f"{name} must not contain code fences")

    titles = re.findall(r"(?m)^# ([^#].*)$", text)
    if titles != [spec.title]:
        raise MemoryPolicyError(
            f"{name} title must be exactly '# {spec.title}'"
        )
    sections = re.findall(r"(?m)^## ([^#].*)$", text)
    if sections != list(spec.sections):
        raise MemoryPolicyError(
            f"{name} sections must be exactly: {', '.join(spec.sections)}"
        )


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        raise MemoryPolicyError("policy YAML frontmatter is missing")
    parts = text.split("---", 2)
    if len(parts) != 3:
        raise MemoryPolicyError("policy YAML frontmatter is unterminated")
    try:
        metadata = yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        raise MemoryPolicyError("policy YAML frontmatter is invalid") from exc
    if not isinstance(metadata, dict):
        raise MemoryPolicyError("policy YAML frontmatter must be a mapping")
    return metadata, parts[2].strip()


def _split_policy_sections(markdown: str) -> dict[str, str]:
    headings: list[tuple[str, int]] = []
    offset = 0
    inside_fence = False
    for line in markdown.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            inside_fence = not inside_fence
        elif not inside_fence and line.startswith("## "):
            headings.append((line[3:].strip(), offset))
        offset += len(line)

    result: dict[str, str] = {}
    for index, (heading, start) in enumerate(headings):
        end = headings[index + 1][1] if index + 1 < len(headings) else len(markdown)
        result[heading] = markdown[start:end].strip()
    return result
