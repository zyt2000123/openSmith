"""Bind Engine-owned runtime capabilities to generic tool providers."""

from __future__ import annotations

import inspect
import logging
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

from .runtime import RuntimeServices

logger = logging.getLogger(__name__)


def bind_memory_ops_tool(services: RuntimeServices, state_dir: Path) -> None:
    memory_dir = state_dir / "memory"
    memory_api = MemoryToolApi()

    def wrapper(func):
        async def execute_with_memory_context(**kwargs):
            kwargs["memory_dir"] = memory_dir
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


def bind_skill_manage_tool(
    services: RuntimeServices,
    state_dir: Path,
    *,
    disabled_skills: frozenset[str] = frozenset(),
    enabled_skills: tuple[str, ...] | None = None,
) -> None:
    """Inject profile-local skill storage into the content-layer manager."""
    from engine.skill.store import SkillStore

    skills_dir = state_dir / "skills"
    store = SkillStore(skills_dir)
    mutating_actions = frozenset({"create", "edit", "patch", "rollback"})

    def wrapper(func):
        async def execute_with_skill_storage(**kwargs):
            action = kwargs.get("action")
            kwargs["agent_skills_dir"] = skills_dir
            kwargs["skill_store"] = store
            result = func(**kwargs)
            output = await result if inspect.isawaitable(result) else result
            if (
                action in mutating_actions
                and isinstance(output, str)
                and output.startswith("OK:")
            ):
                services.skill_registry.load_agent_skills(skills_dir)
                if disabled_skills:
                    services.skill_registry.restrict_to([
                        summary["name"]
                        for summary in services.skill_registry.list_summaries()
                        if summary["name"] not in disabled_skills
                    ])
                if enabled_skills is not None:
                    services.skill_registry.restrict_to(enabled_skills)
            return output

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



def bind_sub_agent_tool(services: RuntimeServices, agents_dir: Path) -> None:
    """Give the generic sub-agent provider the engine's spawn capability.

    A catalog that is empty or malformed leaves the tool registered but hidden:
    the model never sees a capability whose types could not be loaded, and the
    reason is in the log rather than in a runtime error on every turn.
    """
    from engine.execution.subagent import (
        SubAgentCatalog,
        SubAgentCatalogError,
        SubAgentTask,
        run_sub_agents,
    )

    definition = services.tool_registry.get("sub_agent")
    if definition is None:
        return
    try:
        catalog = SubAgentCatalog.load(agents_dir / "subagents")
    except SubAgentCatalogError:
        logger.exception(
            "sub-agent catalog rejected; the sub_agent tool is unavailable this run"
        )
        definition.hidden = True
        return
    if not catalog:
        definition.hidden = True
        return

    agent_ids = list(catalog.ids())
    # Idempotent: appending unconditionally duplicates the whole type listing
    # on every extra bind. Today each turn builds a fresh registry so it never
    # fires, but a binding that corrupts its own tool contract when called
    # twice is a trap for whoever caches services next.
    catalogue_section = f"\n\nAvailable sub-agent types:\n{catalog.describe()}"
    base, marker, _ = definition.description.partition("\n\nAvailable sub-agent types:\n")
    definition.description = (base if marker else definition.description) + catalogue_section
    # The enum keeps an unknown type out of the provider round-trip entirely,
    # rather than spending a sub-agent launch to discover the typo.
    item_properties = (
        definition.parameters.get("properties", {})
        .get("tasks", {})
        .get("items", {})
        .get("properties", {})
    )
    if "agent_type" in item_properties:
        item_properties["agent_type"]["enum"] = agent_ids

    async def spawn(tasks: list[dict], max_parallel: int) -> list[dict]:
        outcomes = await run_sub_agents(
            [
                SubAgentTask(
                    agent_type=task["agent_type"],
                    prompt=task["prompt"],
                    label=task.get("label", ""),
                )
                for task in tasks
            ],
            catalog=catalog,
            llm=services.llm,
            tool_registry=services.tool_registry,
            tool_guard=services.tool_guard,
            # Read at call time, not bind time: preparation finishes wiring the
            # guard, the hooks, and the ports after this binding is installed.
            hook_registry=services.hook_registry,
            background_llm=services.background_llm,
            max_parallel=max_parallel,
        )
        return [asdict(outcome) for outcome in outcomes]

    def wrapper(func):
        async def execute_with_spawn(**kwargs):
            # Injected last so a model-supplied "_spawn" cannot displace the
            # real capability with one of its own.
            kwargs["_spawn"] = spawn
            kwargs["_agent_types"] = tuple(agent_ids)
            return await func(**kwargs)

        return execute_with_spawn

    services.tool_registry.wrap_tool("sub_agent", wrapper)


__all__ = (
    "MemoryToolApi",
    "bind_memory_ops_tool",
    "bind_skill_load_tool",
    "bind_skill_manage_tool",
    "bind_snapshot_tools",
    "bind_sub_agent_tool",
    "bind_todo_tool",
)
