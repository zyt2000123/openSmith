"""Fit complete model requests to a selected route's capacity."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Mapping

from .action_ledger import (
    ActionOutcome,
    DroppedActionLedger,
    is_elision_note,
)
from .budget import (
    ContextBudget,
    context_budget_for,
    estimate_messages_tokens,
    estimate_tool_schema_tokens,
    model_limits_for,
)
from .compression import (
    _preserved_active_context,
    compact_history,
    prune_tool_outputs,
    trim_conversation_for_context_limit,
)

if TYPE_CHECKING:
    from engine.tool.interface import ToolDefinition


class ContextFitStatus(str, Enum):
    FIT = "fit"
    COMPACTED = "compacted"
    RECOVERED = "recovered"
    UNFIT_REQUEST = "unfit_request"
    UNFIT_STATIC_PROMPT = "unfit_static_prompt"
    UNFIT_TOOL_SCHEMAS = "unfit_tool_schemas"


@dataclass(frozen=True, slots=True)
class ContextReceipt:
    message_tokens: int
    tool_schema_tokens: int
    protocol_tokens: int
    estimated_input_tokens: int
    model_context_window: int
    effective_context_window: int
    output_reserve: int
    safety_margin: int
    safe_input_budget: int
    compaction_trigger: int
    window_declared: bool
    output_limit_declared: bool
    estimated: bool = True


@dataclass(frozen=True, slots=True)
class ContextFitResult:
    status: ContextFitStatus
    messages: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...] | None
    receipt: ContextReceipt
    actions: tuple[str, ...] = ()
    prefix_cache_key: str | None = None

    @property
    def fits(self) -> bool:
        return self.status in {
            ContextFitStatus.FIT,
            ContextFitStatus.COMPACTED,
            ContextFitStatus.RECOVERED,
        }


def _shrink_or_drop_elision_notes(
    messages: list[dict[str, Any]],
    *,
    ledger: DroppedActionLedger | None,
    drop: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    changed = False
    updated: list[dict[str, Any]] = []
    for msg in messages:
        if not is_elision_note(msg):
            updated.append(msg)
            continue
        changed = True
        if drop:
            continue
        if ledger is not None:
            compact = ledger.render_elision_note(
                dropped_count=ledger.total_elided_messages,
                dropped_tool_results=ledger.total_elided_tool_results,
                max_entries=10,
                max_bytes=1024,
            )
            if compact["content"] != msg.get("content"):
                updated.append(compact)
            else:
                # Already compact; drop to reclaim space.
                continue
        else:
            # Without a ledger instance, keep only the first line + closing bracket.
            content = str(msg.get("content", ""))
            first_line = content.splitlines()[0] if content else ""
            if first_line and not first_line.endswith("]"):
                first_line = first_line + "]"
            if first_line and first_line != content:
                updated.append({"role": "system", "content": first_line})
    return updated, changed


async def fit_request(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    llm: object,
    *,
    prefix_cache_key: str | None = None,
    allow_model_compaction: bool = True,
    ledger: DroppedActionLedger | None = None,
    call_outcomes: Mapping[str, ActionOutcome] | None = None,
    tool_definitions: Mapping[str, "ToolDefinition"] | None = None,
    task_budget: Any | None = None,
) -> ContextFitResult:
    """Fit and remeasure one complete request before any provider call."""
    budget = context_budget_for(model_limits_for(llm))

    def assess(candidate: list[dict[str, Any]]) -> ContextReceipt:
        return _receipt_for(candidate, tools, budget)

    def result(
        status: ContextFitStatus,
        candidate: list[dict[str, Any]],
        receipt: ContextReceipt,
        actions: list[str],
    ) -> ContextFitResult:
        return ContextFitResult(
            status=status,
            messages=tuple(dict(message) for message in candidate),
            tools=tuple(dict(tool) for tool in tools) if tools else None,
            receipt=receipt,
            actions=tuple(actions),
            prefix_cache_key=prefix_cache_key,
        )

    original = [dict(message) for message in messages]
    original_receipt = measure_request(original, tools, llm)

    # Tool schemas and the leading system contract are irreducible at this
    # layer. Classify them before spending another model call on compaction.
    if _receipt_for([], tools, budget).estimated_input_tokens >= budget.safe_input_budget:
        return result(
            ContextFitStatus.UNFIT_TOOL_SCHEMAS,
            original,
            original_receipt,
            [],
        )
    protected_systems: list[dict[str, Any]] = []
    for message in original:
        if message.get("role") != "system":
            break
        # Dynamic elision annotations are transient and compressible; do not
        # misclassify them as an irreducible static prompt contract.
        if is_elision_note(message):
            continue
        protected_systems.append(message)
    if protected_systems:
        static_receipt = assess(protected_systems)
        if static_receipt.estimated_input_tokens >= budget.safe_input_budget:
            return result(
                ContextFitStatus.UNFIT_STATIC_PROMPT,
                original,
                original_receipt,
                [],
            )
    if not any(message.get("role") != "system" for message in original):
        return result(
            ContextFitStatus.UNFIT_REQUEST,
            original,
            original_receipt,
            [],
        )

    candidate = [dict(message) for message in original]
    actions: list[str] = []
    pruned_chars = prune_tool_outputs(candidate)
    if pruned_chars:
        actions.append(f"pruned_tool_output_chars:{pruned_chars}")

    active_receipt = assess(_preserved_active_context(candidate))
    if active_receipt.estimated_input_tokens > budget.safe_input_budget:
        # Try shrinking or dropping transient elision notes before declaring
        # the active request unfit.
        shrunk, changed = _shrink_or_drop_elision_notes(candidate, ledger=ledger, drop=False)
        if changed:
            candidate = shrunk
            actions.append("elision_note_compacted")
            active_receipt = assess(_preserved_active_context(candidate))
        if active_receipt.estimated_input_tokens > budget.safe_input_budget:
            dropped_notes, dropped_changed = _shrink_or_drop_elision_notes(
                candidate, ledger=ledger, drop=True
            )
            if dropped_changed:
                candidate = dropped_notes
                actions.append("elision_note_dropped")
                active_receipt = assess(_preserved_active_context(candidate))
    if active_receipt.estimated_input_tokens > budget.safe_input_budget:
        return result(
            ContextFitStatus.UNFIT_REQUEST,
            candidate,
            active_receipt,
            actions,
        )

    receipt = assess(candidate)

    if receipt.estimated_input_tokens < budget.compaction_trigger:
        return result(
            ContextFitStatus.COMPACTED if actions else ContextFitStatus.FIT,
            candidate,
            receipt,
            actions,
        )

    if allow_model_compaction and not (
        task_budget is not None and getattr(task_budget, "exhausted", False)
    ):
        try:
            compacted = await compact_history(candidate, llm, task_budget=task_budget)
        except Exception:
            actions.append("compaction_failed")
        else:
            if compacted is not candidate:
                candidate = [dict(message) for message in compacted]
                actions.append("compacted_history")
                receipt = assess(candidate)
                if receipt.estimated_input_tokens <= budget.safe_input_budget:
                    return result(
                        ContextFitStatus.COMPACTED,
                        candidate,
                        receipt,
                        actions,
                    )
            else:
                actions.append("compaction_rejected")
    else:
        actions.append(
            "compaction_skipped_budget"
            if task_budget is not None and getattr(task_budget, "exhausted", False)
            else "model_compaction_disabled"
        )

    receipt = assess(candidate)
    if receipt.estimated_input_tokens <= budget.safe_input_budget:
        return result(
            ContextFitStatus.COMPACTED if pruned_chars else ContextFitStatus.FIT,
            candidate,
            receipt,
            actions,
        )

    # Reserve the exact non-message portions, then make the remaining history
    # fit that message budget. Re-assessment below is the hard postcondition.
    # Deterministic recovery may insert a compacted-history user/assistant
    # pair ahead of the active request. Reserve framing for that complete
    # shape, otherwise an apparently fitting message payload can still exceed
    # the provider budget after protocol overhead is added back.
    recovery_message_count = len(_preserved_active_context(candidate)) + 2
    protocol_reserve = 32 + 4 * recovery_message_count + 8 * len(tools or ())
    message_budget = max(
        1,
        budget.safe_input_budget
        - estimate_tool_schema_tokens(tools)
        - protocol_reserve,
    )
    recovered = trim_conversation_for_context_limit(
        candidate,
        token_budget=message_budget,
        ledger=ledger,
        call_outcomes=call_outcomes,
        tool_definitions=tool_definitions,
    )
    recovered_receipt = assess(recovered)
    actions.append("deterministic_trim")
    if (
        recovered_receipt.estimated_input_tokens <= budget.safe_input_budget
        and any(message.get("role") != "system" for message in recovered)
    ):
        return result(
            ContextFitStatus.RECOVERED,
            recovered,
            recovered_receipt,
            actions,
        )
    return result(
        ContextFitStatus.UNFIT_REQUEST,
        recovered,
        recovered_receipt,
        actions,
    )


def measure_request(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    llm: object,
) -> ContextReceipt:
    """Measure one complete request without mutating it or calling a provider."""
    return _receipt_for(
        messages,
        tools,
        context_budget_for(model_limits_for(llm)),
    )


def _receipt_for(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    budget: ContextBudget,
) -> ContextReceipt:
    tool_schema_tokens = estimate_tool_schema_tokens(tools)
    message_tokens = estimate_messages_tokens(messages)
    protocol_tokens = 32 + 4 * len(messages) + 8 * len(tools or ())
    total = message_tokens + tool_schema_tokens + protocol_tokens
    return ContextReceipt(
        message_tokens=message_tokens,
        tool_schema_tokens=tool_schema_tokens,
        protocol_tokens=protocol_tokens,
        estimated_input_tokens=total,
        model_context_window=budget.model_context_window,
        effective_context_window=budget.effective_context_window,
        output_reserve=budget.output_reserve,
        safety_margin=budget.safety_margin,
        safe_input_budget=budget.safe_input_budget,
        compaction_trigger=budget.compaction_trigger,
        window_declared=budget.window_declared,
        output_limit_declared=budget.output_limit_declared,
    )


__all__ = (
    "ContextFitResult",
    "ContextFitStatus",
    "ContextReceipt",
    "fit_request",
    "measure_request",
)
