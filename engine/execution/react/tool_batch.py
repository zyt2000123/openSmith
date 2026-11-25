"""Parallel execution helpers for read-only tool batches within one ReAct turn.

Only tools that are statically pure reads (``permission_level == "read"``,
``concurrency != "serial"``, ``side_effect == "none"``, ``not is_write_tool``,
``not reads_private_data``) and pass preflight policy + ``PreToolHook`` checks
enter a batch.  Write/serial tools, engine specials, preflight blocks/approvals,
and local-read -> network/egress transitions act as strict barriers so relative
read/write ordering, session taint propagation, RepeatGuard thresholds, and
``HashChainLog`` audit order stay deterministic.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, AsyncGenerator

from engine.safety.tool_policy import ToolPolicyDecision
from engine.tool.interface import ToolCall, ToolDefinition, ToolResult

if TYPE_CHECKING:
    from engine.execution.hooks import HookRegistry
    from engine.tool.registry import ToolRegistry


MAX_PARALLEL_READ_TOOLS = 8
_SPECIAL_SERIAL_TOOLS = frozenset({"get_tool_schema", "render_ui"})


def is_statically_parallel_readable(
    tool_name: str,
    defn: ToolDefinition | None,
) -> bool:
    """Return whether a tool's static contract allows concurrent execution.

    ``reads_private_data`` tools are intentionally excluded because executing
    one taints the session and may alter policy decisions for later calls in
    the same assistant turn.
    """
    if defn is None:
        return False
    if tool_name in _SPECIAL_SERIAL_TOOLS:
        return False
    return (
        defn.permission_level == "read"
        and defn.concurrency != "serial"
        and defn.side_effect == "none"
        and not defn.is_write_tool
        and not defn.reads_private_data
    )


def is_network_or_egress_tool(defn: ToolDefinition | None) -> bool:
    """Return whether a tool may perform outbound network or MCP egress.

    Local read batches must flush and complete ``record_post_execution`` before
    any network/egress tool is even preflight-evaluated, preventing same-turn
    ``[read_file(secret), web_fetch(external)]`` from racing past session taint.
    """
    if defn is None:
        return False
    return bool(
        defn.network_access
        or defn.egress
        or defn.egress_target_arg
        or defn.name.startswith("mcp_")
    )


@dataclass(frozen=True)
class PreparedReadCall:
    """A preflight-approved read-only tool call queued for batch execution."""

    slot: int
    raw_id: str
    raw_name: str
    call: ToolCall
    decision: ToolPolicyDecision
    is_network: bool = False


@dataclass(frozen=True)
class BatchExecutionOutcome:
    """Completed execution result for one call in a read-only batch."""

    slot: int
    item: PreparedReadCall
    result: ToolResult
    warnings: list[str]


async def execute_read_batch(
    batch: list[PreparedReadCall],
    *,
    tool_registry: "ToolRegistry",
    hook_registry: "HookRegistry | None" = None,
    max_parallel: int = MAX_PARALLEL_READ_TOOLS,
) -> AsyncGenerator[BatchExecutionOutcome, None]:
    """Execute preflight-approved read-only calls concurrently, yielding as each completes.

    Each call runs in its own ``asyncio.Task`` so ``ToolRegistry._authorized_call``
    (a ``ContextVar``) stays isolated per task.  Cancellation or generator close
    cancels and awaits every spawned task so no background work leaks.
    """
    if not batch:
        return

    sem = asyncio.Semaphore(max(1, min(max_parallel, MAX_PARALLEL_READ_TOOLS)))
    queue: asyncio.Queue[BatchExecutionOutcome | BaseException] = asyncio.Queue()

    async def _run_one(item: PreparedReadCall) -> None:
        try:
            async with sem:
                with tool_registry.authorize_execution(
                    item.call,
                    approval_id=None,
                    approval_scope=item.decision.approval_scope,
                ):
                    result = await tool_registry.execute(item.call)
                warnings = (
                    await hook_registry.run_post_hooks(
                        item.call.name,
                        item.call.arguments,
                        result,
                    )
                    if hook_registry is not None
                    else []
                )
            await queue.put(
                BatchExecutionOutcome(
                    slot=item.slot,
                    item=item,
                    result=result,
                    warnings=warnings,
                )
            )
        except BaseException as exc:
            await queue.put(exc)

    tasks = [asyncio.create_task(_run_one(item)) for item in batch]
    try:
        for _ in range(len(batch)):
            outcome = await queue.get()
            if isinstance(outcome, BaseException):
                raise outcome
            yield outcome
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
