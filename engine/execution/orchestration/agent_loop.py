"""Core Agent dispatch across direct ReAct, pipelines, and forced skills."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import AsyncGenerator

from engine.execution.events import EventType, ExecutionEvent
from engine.execution.hooks import HookRegistry
from engine.execution.pipeline.backtrack import FailureLoopGuard
from engine.execution.pipeline.pipeline import CTX_PROVISIONAL_OUTPUTS, run_pipeline
from engine.execution.pipeline.pipeline_context import (
    CTX_AGENT_ID,
    CTX_CHAIN_REQUEST,
    CTX_FORCED_SKILL,
    CTX_IDENTITY_ID,
    CTX_ROUTE_ID,
    CTX_RUN_ID,
    CTX_SESSION_ID,
    CTX_STATE_DIR,
    CTX_TASK_TYPE,
    CTX_USER_MESSAGE,
    CTX_USER_RESPONSE,
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
    hook_registry: HookRegistry | None = None,
) -> AsyncGenerator[ExecutionEvent, None]:
    """Route to the selected execution implementation and yield events."""
    # Matt's user-facing `grill-me` is an entry wrapper around `grilling`.
    # In Agent-Smith it enters the full requirements chain rather than
    # bypassing it as a one-off forced skill invocation.
    grill_me_chain_entry = (
        forced_skill == "grill-me"
        and route.route_id == "requirements-research"
        and route.pipeline_id == "requirements-research"
        and skill_chain is not None
    )
    if forced_skill and not grill_me_chain_entry:
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
            hook_registry=hook_registry,
        ):
            yield event
        yield ExecutionEvent(EventType.DONE, {})
        return

    context: dict = {
        CTX_USER_MESSAGE: user_message,
        CTX_CHAIN_REQUEST: user_message,
        CTX_IDENTITY_ID: route.identity_id,
        CTX_ROUTE_ID: route.route_id,
    }
    if execution_context:
        context.update(
            {key: value for key, value in execution_context.items() if value is not None}
        )

    context, start_node_idx = _apply_session_checkpoint(
        context,
        route.route_id or "",
        user_message,
        [node.skill_name for node in skill_chain.nodes],
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


def _checkpoint_owner_still_running(
    state_dir: str, owner_run_id: str, current_run_id: str
) -> bool:
    """True when the run that wrote a checkpoint is still executing.

    Request identity alone cannot distinguish "the previous run crashed" from
    "the previous run is still going", so re-submitting an identical message
    (a client retry after a timeout, a second click) used to adopt the live
    run's half-finished state — both runs then wrote the same working_dir with
    no coordination between them.
    """
    if not owner_run_id or owner_run_id == current_run_id:
        return False
    try:
        from .run_state import RunStateStore, RunStatus

        state = RunStateStore(Path(state_dir)).get(owner_run_id)
    except Exception:
        # Unable to prove the owner is finished — refuse to adopt its state.
        logger.warning("cannot inspect checkpoint owner %s", owner_run_id, exc_info=True)
        return True
    if state is None:
        return False
    # QUEUED counts as live too: that run has not executed yet, so handing its
    # checkpoint to someone else is the same race, just earlier.
    return state.status in {
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        RunStatus.WAITING_APPROVAL,
    }


def _resume_node_still_matches(checkpoint, node_skills: list[str]) -> bool:
    """Whether the checkpoint's node is still that node in the reloaded chain.

    ``skill_chain_index`` is a position, not an identity.  Every run reloads
    the chain from YAML (user overrides in ``~/.agent-smith/pipelines/`` win),
    so a pipeline edited while a node waited for an answer keeps the index
    valid while moving it onto a different node: the pending answer is then fed
    to a node that never asked anything, and after a deletion crash recovery's
    ``index + 1`` skips a node that never ran.  A checkpoint written before
    ``node_skill`` existed carries no name to compare — see the field's comment
    for why that resumes instead of being discarded.
    """
    index = checkpoint.skill_chain_index
    if not 0 <= index < len(node_skills):
        return False
    return not checkpoint.node_skill or checkpoint.node_skill == node_skills[index]


def _apply_session_checkpoint(
    context: dict,
    route_id: str,
    user_message: str,
    node_skills: list[str],
) -> tuple[dict, int]:
    """Resume a crash checkpoint or a deliberate user-input pause.

    Crash recovery remains exact-message only.  A checkpoint marked
    ``awaiting_user_input`` is different: its next session message is the
    answer to the node's one pending question, so it resumes the same node
    while preserving the original request under ``chain_request``.
    """
    session_id = str(context.get(CTX_SESSION_ID) or "")
    state_dir = str(context.get(CTX_STATE_DIR) or "")
    if not session_id or not state_dir:
        return context, 0
    expected_agent_id = str(context.get(CTX_AGENT_ID) or "")
    expected_identity_id = str(context.get(CTX_IDENTITY_ID) or "")
    expected_working_dir = str(context.get(CTX_WORKING_DIR) or "")
    current_run_id = str(context.get(CTX_RUN_ID) or "")
    try:
        from engine.execution.pipeline.checkpoint import SessionStateManager

        manager = SessionStateManager(Path(state_dir))
        checkpoint = manager.restore(session_id)
        if checkpoint is None:
            return context, 0
        if checkpoint.run_id and _checkpoint_owner_still_running(
            state_dir, checkpoint.run_id, current_run_id
        ):
            # Another run owns this session and has not finished.  Start fresh,
            # but leave its checkpoint in place: falling through to the clear()
            # below would delete the crash-recovery point of a run that is still
            # executing — exactly the state this check exists to protect.  The
            # same applies when ownership could not be determined, since
            # _checkpoint_owner_still_running fails closed.
            logger.info(
                "session %s: checkpoint owned by live run %s — starting fresh, "
                "keeping their checkpoint",
                session_id,
                checkpoint.run_id,
            )
            return context, 0
        same_scope = (
            expected_agent_id
            and expected_identity_id
            and expected_working_dir
            and checkpoint.run_id
            and checkpoint.agent_id == expected_agent_id
            and checkpoint.identity_id == expected_identity_id
            and checkpoint.working_dir == expected_working_dir
            and checkpoint.route_id == route_id
            and _resume_node_still_matches(checkpoint, node_skills)
        )
        if checkpoint.awaiting_user_input and same_scope:
            logger.info(
                "session %s: resuming node %d with a user response",
                session_id,
                checkpoint.skill_chain_index,
            )
            restored = {**checkpoint.context, **context}
            restored[CTX_CHAIN_REQUEST] = checkpoint.context.get(
                CTX_CHAIN_REQUEST,
                checkpoint.context.get(CTX_USER_MESSAGE, ""),
            )
            restored[CTX_USER_RESPONSE] = user_message
            # P6: carry the provisional-streaming ledger so the resumed run does
            # not re-render text that was already streamed before the pause.
            restored[CTX_PROVISIONAL_OUTPUTS] = dict(checkpoint.provisional_outputs)
            return restored, checkpoint.skill_chain_index
        if (
            same_scope
            and checkpoint.context.get(
                CTX_CHAIN_REQUEST,
                checkpoint.context.get(CTX_USER_MESSAGE),
            ) == user_message
        ):
            logger.info(
                "session %s: resuming crashed chain, skipping %d completed node(s)",
                session_id,
                checkpoint.skill_chain_index + 1,
            )
            restored = {**checkpoint.context, **context}
            restored[CTX_PROVISIONAL_OUTPUTS] = dict(checkpoint.provisional_outputs)
            return restored, checkpoint.skill_chain_index + 1
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
    terminal_event: ExecutionEvent | None = None
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
        if event.type in {EventType.INCOMPLETE, EventType.FAILED}:
            terminal_type = (
                "incomplete" if event.type is EventType.INCOMPLETE else "failed"
            )
            # P12: a consumer that treats FAILED/INCOMPLETE as a stop point
            # would never render the accumulated text if it arrived later.
            # Hold the terminal event back until the text has been emitted.
            terminal_event = event
            continue
        yield event
    if terminal_type:
        data: dict[str, object] = {"text": "".join(output_parts)}
        if output_was_streamed:
            data["already_streamed"] = True
        if output_parts:
            yield ExecutionEvent(EventType.TEXT_DELTA, data)
        if terminal_event is not None:
            yield terminal_event
        yield ExecutionEvent(
            EventType.SKILL_END,
            {"skill": forced_skill, "status": terminal_type},
        )
        yield ExecutionEvent(EventType.DONE, {})
        return

    yield ExecutionEvent(
        EventType.SKILL_END,
        {"skill": forced_skill, "status": "passed"},
    )
    # Guarded like the terminal branch above: an empty TEXT_DELTA is not a
    # reply, and consumers persist whatever text they receive.
    if output_parts:
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
