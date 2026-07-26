"""Bind Engine-owned runtime capabilities to generic tool providers."""

from __future__ import annotations

import inspect
from hashlib import sha256
from pathlib import Path

from .runtime import RuntimeServices


def bind_memory_ops_tool(services: RuntimeServices, state_dir: Path) -> None:
    memory_dir = state_dir / "memory"
    memory_api = MemoryToolApi()

    async def episode_runner(memory_dir: Path, topic: str, related: list[dict]):
        from engine.memory.compile import compact_episode

        return await compact_episode(
            memory_dir,
            services.llm,
            topic,
            related,
            reviewer=services.gate_llm,
        )

    def wrapper(func):
        async def execute_with_memory_context(**kwargs):
            kwargs["memory_dir"] = memory_dir
            kwargs["episode_runner"] = episode_runner
            kwargs["memory_api"] = memory_api
            return await func(**kwargs)

        return execute_with_memory_context

    services.tool_registry.wrap_tool("memory_ops", wrapper)


class MemoryToolApi:
    """Engine-owned memory capability injected into the generic tool provider."""

    def __init__(self) -> None:
        from engine import memory

        self.MANUAL_MEMORY_KINDS = memory.MANUAL_MEMORY_KINDS
        self.MANUAL_EVIDENCE_TYPES = memory.MANUAL_EVIDENCE_TYPES
        self.MEMORY_LAYER_FILES = memory.MEMORY_LAYER_FILES
        self.contains_secret = memory.contains_secret
        self.contains_injection = memory.contains_injection
        self.sanitize_memory_text = memory.sanitize_memory_text
        self.sanitize_event_value = memory.sanitize_event_value
        self.safe_file_in_dir = memory.safe_file_in_dir
        self.safe_markdown_files = memory.safe_markdown_files
        self.atomic_write_text = memory.atomic_write_text

    async def remove_episode_from_index(self, memory_dir: Path, episode_id: str) -> None:
        from engine.memory.search import SearchIndex

        index = SearchIndex(memory_dir / "episodes")
        await index.open()
        try:
            await index.remove_entry(episode_id)
        finally:
            await index.close()


def bind_snapshot_tools(services: RuntimeServices, session_id: str | None) -> None:
    """Inject session-scoped snapshots into generic write/edit tool content."""
    from engine.tool.snapshot import get_snapshot

    tracker = get_snapshot(session_id or "default").track

    def wrapper(func):
        async def execute_with_snapshot(**kwargs):
            kwargs["_snapshot_tracker"] = tracker
            result = func(**kwargs)
            return await result if inspect.isawaitable(result) else result

        return execute_with_snapshot

    for tool_name in ("write_file", "edit_file"):
        services.tool_registry.wrap_tool(tool_name, wrapper)


def bind_skill_manage_tool(services: RuntimeServices, state_dir: Path) -> None:
    """Inject profile-local skill storage into the content-layer manager."""
    from engine.skill.store import SkillStore

    skills_dir = state_dir / "skills"
    store = SkillStore(skills_dir)

    def wrapper(func):
        async def execute_with_skill_storage(**kwargs):
            kwargs["agent_skills_dir"] = skills_dir
            kwargs["skill_store"] = store
            result = func(**kwargs)
            return await result if inspect.isawaitable(result) else result

        return execute_with_skill_storage

    services.tool_registry.wrap_tool("skill_manage", wrapper)


def bind_skill_load_tool(services: RuntimeServices) -> None:
    """Expose only the same per-request registry used for prompt and execution."""

    def load_skill(name: str) -> tuple[str | None, list[str]]:
        skill = services.skill_registry.get(name)
        available = sorted(
            summary["name"] for summary in services.skill_registry.list_summaries()
        )
        return (skill.content if skill is not None else None, available)

    def wrapper(func):
        async def execute_with_skill_catalog(**kwargs):
            kwargs["skill_loader"] = load_skill
            result = func(**kwargs)
            return await result if inspect.isawaitable(result) else result

        return execute_with_skill_catalog

    services.tool_registry.wrap_tool("skill_load", wrapper)


def bind_todo_tool(
    services: RuntimeServices,
    state_dir: Path,
    session_id: str | None,
) -> None:
    """Persist Todo state per session rather than per imported tool module."""
    token = sha256((session_id or "default").encode("utf-8")).hexdigest()
    todo_file = state_dir / "todos" / f"{token}.json"

    def wrapper(func):
        async def execute_with_session_todos(**kwargs):
            kwargs["todo_file"] = todo_file
            return await func(**kwargs)

        return execute_with_session_todos

    services.tool_registry.wrap_tool("todo", wrapper)


__all__ = (
    "MemoryToolApi",
    "bind_memory_ops_tool",
    "bind_skill_load_tool",
    "bind_skill_manage_tool",
    "bind_snapshot_tools",
    "bind_todo_tool",
)
