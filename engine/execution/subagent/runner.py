"""Run sub-agents: isolated ReAct conversations that return only a summary.

Each task gets a fresh conversation — the parent's history, memory, and skill
prompt are deliberately *not* inherited.  That isolation is the whole point:
the parent spends the tokens of a summary, not of the sub-agent's transcript.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from engine.execution.events import EventType, ExecutionEvent
from engine.tool.registry import ScopedToolRegistry

from .catalog import (
    DEFAULT_MODEL_ROLE,
    SUB_AGENT_TOOL_NAME,
    SubAgentCatalog,
    SubAgentCatalogError,
    SubAgentSpec,
)

if TYPE_CHECKING:
    from engine.execution.hooks import HookRegistry
    from engine.llm.port import LLMPort
    from engine.safety.tool_guard import ToolGuard
    from engine.tool.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Fan-out ceilings.  The model picks a width; these decide what the model is
# allowed to pick.  A parent turn that spawns 30 agents is a runaway, not a
# strategy.
MAX_TASKS_PER_CALL = 10
DEFAULT_MAX_PARALLEL = 4
MAX_PARALLEL_CEILING = 8
# Wall clock per sub-agent.  Without it one stuck provider call pins the
# parent turn open indefinitely, and the user sees a hang with no output.
DEFAULT_TASK_TIMEOUT_SECONDS = 600.0
# Ceiling on what one fan-out may spend in total.  Per-agent budgets bound
# each participant; this bounds the turn, which is the number that shows up
# on the bill.
DEFAULT_BATCH_TOKEN_BUDGET = 600_000

_REPORT_CONTRACT = """
## Reporting contract

You are a sub-agent. The agent that spawned you sees **only your final
message** — never your tool calls, their output, or your reasoning. Work the
task, then finish with a self-contained report:

1. **Answer** — the direct result, first.
2. **Evidence** — concrete `file:line` references, commands run, values found.
3. **Gaps** — anything you could not determine, stated plainly.

Do not ask the parent questions; it cannot reply. If the task is
under-specified, state the assumption you made and continue. Never claim
something you did not verify.
""".strip()


class _BatchBudget:
    """Tokens the whole fan-out may still spend.

    Single-threaded asyncio: ``spend`` runs to completion between awaits, so
    the counter needs no lock. Exhaustion is sticky — once the batch is over
    budget, every still-running agent stops at its next checkpoint rather than
    racing to spend the remainder.
    """

    def __init__(self, total: int) -> None:
        self.total = total
        self.spent = 0

    def spend(self, tokens: int) -> None:
        self.spent += tokens

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.total


@dataclass(frozen=True)
class SubAgentTask:
    """One unit of delegated work."""

    agent_type: str
    prompt: str
    label: str = ""

    def display(self, index: int) -> str:
        return self.label.strip() or f"{self.agent_type}#{index + 1}"


@dataclass
class SubAgentOutcome:
    """What a finished sub-agent hands back to the parent."""

    label: str
    agent_type: str
    ok: bool
    summary: str = ""
    error: str = ""
    tool_calls: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    model_role: str = ""


def _conversation(spec: SubAgentSpec, task: SubAgentTask) -> list[dict]:
    # The tool list is stated explicitly because a sub-agent's prompt is not
    # built by PromptAssembler and therefore carries no "Available Tools"
    # layer.  Under the runtime's lazy-schema mode the model is handed only a
    # schema *loader* and would otherwise have to guess the names to load.
    toolbox = (
        "## Your tools\n"
        f"You may call exactly these: {', '.join(spec.tools)}. "
        "Nothing else is available to you."
    )
    system = f"{spec.prompt.strip()}\n\n{toolbox}\n\n{_REPORT_CONTRACT}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": task.prompt.strip()},
    ]


def _accumulate_usage(total: dict[str, int], event: ExecutionEvent) -> int:
    """Fold one usage event into *total*; return the tokens it added.

    Providers that report only input/output are charged the sum, so a missing
    ``total_tokens`` cannot make a run look free to the budget.
    """
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = event.data.get(key)
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value
    charged = event.data.get("total_tokens")
    if not isinstance(charged, int) or charged <= 0:
        charged = sum(
            value
            for key in ("input_tokens", "output_tokens")
            if isinstance(value := event.data.get(key), int)
        )
    return max(0, charged)


async def _run_one(
    task: SubAgentTask,
    index: int,
    *,
    catalog: SubAgentCatalog,
    llm_for_role: dict[str, "LLMPort"],
    tool_registry: "ToolRegistry",
    tool_guard: "ToolGuard | None",
    hook_registry: "HookRegistry | None",
    timeout_seconds: float,
    batch: _BatchBudget,
) -> SubAgentOutcome:
    label = task.display(index)
    try:
        spec = catalog.get(task.agent_type)
    except SubAgentCatalogError as exc:
        return SubAgentOutcome(label, task.agent_type, ok=False, error=str(exc))

    # An unavailable role falls back to the interactive port rather than
    # failing the task: a deployment that never built a background client
    # should still be able to run a type that prefers one.
    llm = llm_for_role.get(spec.model) or llm_for_role[DEFAULT_MODEL_ROLE]

    # Belt and braces: the catalog already strips the spawn tool, but a spec
    # built directly (tests, embeddings) must not be able to recurse either.
    scoped = ScopedToolRegistry(
        tool_registry,
        [name for name in spec.tools if name != SUB_AGENT_TOOL_NAME],
    )
    text_parts: list[str] = []
    usage: dict[str, int] = {}
    tool_calls = 0
    failure = ""

    from engine.execution.react.react_loop import react_event_loop
    from engine.safety.approval import without_approval_context
    from engine.safety.fact_gate import (
        FactGate,
        FactGateContext,
        current_fact_gate,
        use_fact_gate,
    )

    # A fresh gate per sub-agent. The parent's instance carries mutable
    # per-turn state (`_checked` / `_pending`) that `begin_round()` rewrites;
    # sharing it across a fan-out lets one agent's round boundary silently
    # satisfy a sibling's outstanding challenge. Scoped to this sub-agent's
    # own tool set, which is narrower than the parent's.
    parent_gate = current_fact_gate()
    sub_gate = (
        FactGate(
            FactGateContext(
                session_id=parent_gate.context.session_id,
                turn_id=f"{parent_gate.context.turn_id}:sub:{index}",
            ),
            enabled=parent_gate.enabled,
            tool_registry=scoped.definitions(),
        )
        if parent_gate is not None
        else None
    )

    def outcome(ok: bool, *, error: str = "") -> SubAgentOutcome:
        return SubAgentOutcome(
            label,
            task.agent_type,
            ok=ok,
            summary="".join(text_parts).strip(),
            error=error,
            tool_calls=tool_calls,
            usage=usage,
            model_role=spec.model,
        )

    if batch.exhausted:
        return outcome(False, error="batch token budget exhausted before start")

    try:
        # ``use_fact_gate`` also displaces the parent's gate for anything in
        # this task that reads the ContextVar directly.
        with without_approval_context(), use_fact_gate(sub_gate):
            async with asyncio.timeout(timeout_seconds):
                # ``aclosing`` finalizes the loop deterministically when a
                # budget break leaves it suspended, instead of waiting on the
                # garbage collector to close it.
                async with aclosing(
                    react_event_loop(
                        llm,
                        _conversation(spec, task),
                        scoped,
                        tool_guard,
                        spec.max_iters,
                        # A sub-agent's text never reaches the user's screen,
                        # so the provisional commit/retract machinery has
                        # nothing to drive.
                        provisional_lifecycle=False,
                        # Without this the built-in PreToolHooks — including
                        # config-protection — simply do not run for delegated
                        # work, so a sub-agent could edit files the parent is
                        # blocked from touching.
                        hook_registry=hook_registry,
                    )
                ) as events:
                    async for event in events:
                        if event.type is EventType.TEXT_DELTA:
                            text = event.data.get("text")
                            if isinstance(text, str) and text:
                                text_parts.append(text)
                        elif event.type is EventType.TOOL_CALL_START:
                            tool_calls += 1
                        elif event.type is EventType.TOKEN_USAGE:
                            batch.spend(_accumulate_usage(usage, event))
                            # Checked after the spend that caused it: the
                            # tokens are already billed, so the only useful
                            # response is to stop before the next turn
                            # compounds them.
                            if usage.get("total_tokens", 0) >= spec.token_budget:
                                failure = (
                                    f"token budget exhausted "
                                    f"({usage['total_tokens']}/{spec.token_budget})"
                                )
                                break
                            if batch.exhausted:
                                failure = "batch token budget exhausted"
                                break
                        elif event.type is EventType.FAILED:
                            failure = str(event.data.get("reason") or "failed")
                        elif event.type is EventType.INCOMPLETE:
                            failure = str(event.data.get("reason") or "incomplete")
    except asyncio.CancelledError:
        # Parent turn aborted — propagate so the whole fan-out unwinds instead
        # of reporting a fake per-task failure.
        raise
    except TimeoutError:
        return outcome(False, error=f"timed out after {timeout_seconds:.0f}s")
    except Exception as exc:
        # One task must not sink the batch.
        logger.warning("sub-agent %s failed", label, exc_info=True)
        return outcome(False, error=f"{type(exc).__name__}: {exc}")

    if not "".join(text_parts).strip():
        # A sub-agent whose only product is a summary and that produced none
        # did not succeed, whatever the loop reported.
        failure = failure or "sub-agent produced no report"
    return outcome(not failure, error=failure)


async def run_sub_agents(
    tasks: list[SubAgentTask],
    *,
    catalog: SubAgentCatalog,
    llm: "LLMPort",
    tool_registry: "ToolRegistry",
    tool_guard: "ToolGuard | None" = None,
    hook_registry: "HookRegistry | None" = None,
    background_llm: "LLMPort | None" = None,
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    timeout_seconds: float = DEFAULT_TASK_TIMEOUT_SECONDS,
    batch_token_budget: int = DEFAULT_BATCH_TOKEN_BUDGET,
) -> list[SubAgentOutcome]:
    """Run *tasks* concurrently and return one outcome per task, in order."""
    if not tasks:
        return []
    if len(tasks) > MAX_TASKS_PER_CALL:
        raise ValueError(
            f"at most {MAX_TASKS_PER_CALL} sub-agent tasks per call, got {len(tasks)}"
        )
    width = max(1, min(int(max_parallel), MAX_PARALLEL_CEILING, len(tasks)))
    limiter = asyncio.Semaphore(width)
    batch = _BatchBudget(max(1, int(batch_token_budget)))
    llm_for_role: dict[str, "LLMPort"] = {DEFAULT_MODEL_ROLE: llm}
    if background_llm is not None:
        llm_for_role["background"] = background_llm

    async def guarded(task: SubAgentTask, index: int) -> SubAgentOutcome:
        async with limiter:
            return await _run_one(
                task,
                index,
                catalog=catalog,
                llm_for_role=llm_for_role,
                tool_registry=tool_registry,
                tool_guard=tool_guard,
                hook_registry=hook_registry,
                timeout_seconds=timeout_seconds,
                batch=batch,
            )

    results = await asyncio.gather(
        *(guarded(task, index) for index, task in enumerate(tasks)),
        return_exceptions=True,
    )
    outcomes: list[SubAgentOutcome] = []
    for index, (task, result) in enumerate(zip(tasks, results, strict=True)):
        if isinstance(result, BaseException):
            if isinstance(result, asyncio.CancelledError):
                raise result
            outcomes.append(
                SubAgentOutcome(
                    task.display(index),
                    task.agent_type,
                    ok=False,
                    error=f"{type(result).__name__}: {result}",
                )
            )
        else:
            outcomes.append(result)
    return outcomes


__all__ = (
    "DEFAULT_BATCH_TOKEN_BUDGET",
    "DEFAULT_MAX_PARALLEL",
    "DEFAULT_TASK_TIMEOUT_SECONDS",
    "MAX_PARALLEL_CEILING",
    "MAX_TASKS_PER_CALL",
    "SubAgentOutcome",
    "SubAgentTask",
    "run_sub_agents",
)
