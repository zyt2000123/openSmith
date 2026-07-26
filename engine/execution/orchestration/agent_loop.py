"""Core Agent dispatch across direct ReAct, pipelines, and forced skills."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import AsyncGenerator

from engine.execution.events import EventType, ExecutionEvent
from engine.execution.pipeline.backtrack import FailureLoopGuard
from engine.execution.pipeline.pipeline import run_pipeline
from engine.execution.pipeline.pipeline_context import (
    CTX_AGENT_ID,
    CTX_FORCED_SKILL,
    CTX_IDENTITY_ID,
    CTX_ROUTE_ID,
    CTX_SESSION_ID,
    CTX_STATE_DIR,
    CTX_TASK_TYPE,
    CTX_USER_MESSAGE,
    CTX_WORKING_DIR,
)
from engine.execution.pipeline.skill_chain import SkillChain
from engine.execution.react.budget import DEFAULT_MAX_REACT_ITERS
from engine.execution.react.react_loop import react_event_loop
from engine.identity import RouteDecision
from engine.llm.port import LLMPort
from engine.safety.tool_guard import ToolGuard
from engine.skill.executor import execute_skill_events
from engine.skill.registry import SkillRegistry
from engine.tool.registry import ToolRegistry


logger = logging.getLogger(__name__)


async def run_agent_stream(
    llm: LLMPort,
    system_prompt: str,
    user_message: str,
    tool_registry: ToolRegistry,
    skill_registry: SkillRegistry,
    route: RouteDecision,
    skill_chain: SkillChain | None,
    guard: FailureLoopGuard,
    tool_guard: ToolGuard | None = None,
    max_react_iters: int = DEFAULT_MAX_REACT_ITERS,
    history: list[dict] | None = None,
    forced_skill: str | None = None,
    execution_context: dict | None = None,
    gate_llm: LLMPort | None = None,
    disabled_skill_names: frozenset[str] = frozenset(),
    prefix_cache_key: str | None = None,
) -> AsyncGenerator[ExecutionEvent, None]:
    """Route to the selected execution implementation and yield events."""
    if forced_skill:
        async for event in _run_forced_skill_stream(
            llm,
            system_prompt,
            tool_registry,
            skill_registry,
            user_message,
            forced_skill,
            tool_guard,
            max_react_iters,
            history=history,
            execution_context=execution_context,
            prefix_cache_key=prefix_cache_key,
        ):
            yield event
        return

    base_messages = [
        {"role": "system", "content": system_prompt},
        *(history or []),
        {"role": "user", "content": user_message},
    ]

    yield ExecutionEvent(EventType.ROUTE_DECIDED, route.to_event_data())

    if route.pipeline_id is None or skill_chain is None:
        async for event in react_event_loop(
            llm,
            base_messages,
            tool_registry,
            tool_guard,
            max_react_iters,
            prefix_cache_key=prefix_cache_key,
        ):
            yield event
        yield ExecutionEvent(EventType.DONE, {})
        return

    # A pipeline with missing installed skills cannot satisfy its gate
    # contracts. Fall back to direct ReAct; user-disabled nodes are skipped.
    missing_skills = sorted(
        {
            node.skill_name
            for node in skill_chain.nodes
            if (
                node.skill_name not in disabled_skill_names
                and skill_registry.get(node.skill_name) is None
            )
        }
    )
    if missing_skills:
        logger.warning(
            "pipeline %r unavailable because skills are not installed: %s; "
            "falling back to direct ReAct",
            route.pipeline_id,
            ", ".join(missing_skills),
        )
        async for event in react_event_loop(
            llm,
            base_messages,
            tool_registry,
            tool_guard,
            max_react_iters,
            prefix_cache_key=prefix_cache_key,
        ):
            yield event
        yield ExecutionEvent(EventType.DONE, {})
        return

    context: dict = {
        CTX_USER_MESSAGE: user_message,
        CTX_IDENTITY_ID: route.identity_id,
        CTX_ROUTE_ID: route.route_id,
    }
    if execution_context:
        context.update(
            {key: value for key, value in execution_context.items() if value is not None}
        )

    context, start_node_idx = _apply_crash_checkpoint(
        context,
        route.route_id or "",
        user_message,
        len(skill_chain.nodes),
    )

    async for event in run_pipeline(
        skill_chain,
        llm,
        user_message,
        base_messages,
        tool_registry,
        skill_registry,
        tool_guard,
        guard,
        max_react_iters,
        context,
        gate_llm=gate_llm,
        start_node_idx=start_node_idx,
        disabled_skill_names=disabled_skill_names,
        prefix_cache_key=prefix_cache_key,
    ):
        yield event


def _apply_crash_checkpoint(
    context: dict,
    route_id: str,
    user_message: str,
    node_count: int,
) -> tuple[dict, int]:
    """Resume a matching crash checkpoint and discard stale state."""
    session_id = str(context.get(CTX_SESSION_ID) or "")
    state_dir = str(context.get(CTX_STATE_DIR) or "")
    if not session_id or not state_dir:
        return context, 0
    expected_agent_id = str(context.get(CTX_AGENT_ID) or "")
    expected_identity_id = str(context.get(CTX_IDENTITY_ID) or "")
    expected_working_dir = str(context.get(CTX_WORKING_DIR) or "")
    try:
        from engine.execution.pipeline.checkpoint import SessionStateManager

        manager = SessionStateManager(Path(state_dir))
        checkpoint = manager.restore(session_id)
        if checkpoint is None:
            return context, 0
        if (
            expected_agent_id
            and expected_identity_id
            and expected_working_dir
            and checkpoint.agent_id == expected_agent_id
            and checkpoint.identity_id == expected_identity_id
            and checkpoint.working_dir == expected_working_dir
            and checkpoint.route_id == route_id
            and checkpoint.context.get(CTX_USER_MESSAGE) == user_message
            and 0 <= checkpoint.skill_chain_index < node_count
        ):
            logger.info(
                "session %s: resuming crashed chain, skipping %d completed node(s)",
                session_id,
                checkpoint.skill_chain_index + 1,
            )
            return (
                {**checkpoint.context, **context},
                checkpoint.skill_chain_index + 1,
            )
        manager.clear(session_id)
    except Exception:
        logger.exception("failed to inspect crash checkpoint; starting fresh")
    return context, 0


async def _run_forced_skill_stream(
    llm: LLMPort,
    system_prompt: str,
    tool_registry: ToolRegistry,
    skill_registry: SkillRegistry,
    user_message: str,
    forced_skill: str,
    tool_guard: ToolGuard | None,
    max_react_iters: int,
    history: list[dict] | None = None,
    execution_context: dict | None = None,
    prefix_cache_key: str | None = None,
) -> AsyncGenerator[ExecutionEvent, None]:
    yield ExecutionEvent(
        EventType.ROUTE_DECIDED,
        {"type": "skill", "skill": forced_skill},
    )

    skill = skill_registry.get(forced_skill)
    if skill is None:
        message = _missing_skill_message(skill_registry, forced_skill)
        yield ExecutionEvent(
            EventType.BLOCKED,
            {"skill": forced_skill, "reason": message},
        )
        yield ExecutionEvent(EventType.TEXT_DELTA, {"text": message})
        yield ExecutionEvent(EventType.DONE, {})
        return

    yield ExecutionEvent(EventType.SKILL_START, {"skill": forced_skill, "index": 0})
    messages = [
        {"role": "system", "content": system_prompt},
        *(history or []),
        {"role": "user", "content": user_message},
    ]
    context: dict = {
        CTX_USER_MESSAGE: user_message,
        CTX_TASK_TYPE: "skill",
        CTX_FORCED_SKILL: forced_skill,
    }
    if execution_context:
        context.update(
            {key: value for key, value in execution_context.items() if value is not None}
        )
    output_parts: list[str] = []
    output_was_streamed = False
    terminal_type: str | None = None
    async for event in execute_skill_events(
        skill,
        llm,
        tool_registry,
        messages,
        context,
        max_react_iters,
        tool_guard=tool_guard,
        react_event_loop_fn=react_event_loop,
        prefix_cache_key=prefix_cache_key,
    ):
        if event.type == EventType.TEXT_DELTA:
            output_parts.append(str(event.data.get("text", "")))
            output_was_streamed = output_was_streamed or bool(
                event.data.get("already_streamed")
            )
            continue
        if event.type == EventType.INCOMPLETE:
            terminal_type = "incomplete"
        elif event.type == EventType.FAILED:
            terminal_type = "failed"
        yield event
    if terminal_type:
        yield ExecutionEvent(
            EventType.SKILL_END,
            {"skill": forced_skill, "status": terminal_type},
        )
        if output_parts:
            data: dict[str, object] = {"text": "".join(output_parts)}
            if output_was_streamed:
                data["already_streamed"] = True
            yield ExecutionEvent(EventType.TEXT_DELTA, data)
        yield ExecutionEvent(EventType.DONE, {})
        return

    yield ExecutionEvent(
        EventType.SKILL_END,
        {"skill": forced_skill, "status": "passed"},
    )
    data = {"text": "".join(output_parts)}
    if output_was_streamed:
        data["already_streamed"] = True
    yield ExecutionEvent(EventType.TEXT_DELTA, data)
    yield ExecutionEvent(EventType.DONE, {})


def _missing_skill_message(
    skill_registry: SkillRegistry,
    forced_skill: str,
) -> str:
    available = ", ".join(
        sorted(summary["name"] for summary in skill_registry.list_summaries())
    )
    if not available:
        return f"Skill '{forced_skill}' not found. No skills are currently available."
    return f"Skill '{forced_skill}' not found. Available skills: {available}"


__all__ = ("run_agent_stream",)
