"""Skill loader tool provider — reads the runtime's registered skill catalog."""
# 仅按注册名加载技能定义，避免模型把磁盘上的任意 Markdown 当作工作流指令。

import asyncio
from collections.abc import Callable

TOOL_META = {
    "name": "skill_load",
    # Model-facing on purpose: the prompt advertises a skill catalog and
    # workflow.md tells Smith to load a matching skill on demand, and this is
    # the only opener.  Hiding it left direct ReAct able to list every skill
    # and open none.  The safety property is the name lookup against the
    # request-scoped registry (see builtin_tools.bind_skill_load_tool), not
    # invisibility: arbitrary Markdown on disk stays unreachable either way.
    "hidden": False,
    "description": "Load a skill definition (SKILL.md) by name. Returns the skill's process and guidelines.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Skill name (e.g., 'grilling', 'code-review', 'tdd-workflow')"
            }
        },
        "required": ["name"]
    },
    "permission_level": "read",
    "approval_policy": "never",
    "side_effect": "none",
    "execution_environment": "host",
}

SkillLoader = Callable[[str], tuple[str | None, list[str]]]


def _execute_sync(*, name: str, skill_loader: SkillLoader | None = None) -> str:
    safe_name = name.strip()
    if not safe_name or "/" in safe_name or "\\" in safe_name:
        return "Error: invalid skill name"
    if skill_loader is None:
        return "Error: runtime skill catalog was not provided"

    content, available = skill_loader(safe_name)
    if content is None:
        available_str = ", ".join(available) if available else "(none found)"
        return f"Error: skill '{safe_name}' not found\nAvailable skills: {available_str}"
    return f"# Skill: {safe_name}\n\n{content}"


async def execute(*, name: str, skill_loader: SkillLoader | None = None) -> str:
    return await asyncio.to_thread(
        _execute_sync,
        name=name,
        skill_loader=skill_loader,
    )
