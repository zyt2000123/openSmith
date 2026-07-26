"""Fit complete model requests to a selected route's capacity."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .budget import (
    ContextBudget,
    context_budget_for,
    estimate_messages_tokens,
    estimate_tool_schema_tokens,
    model_limits_for,
)
from .compression import (
    compact_history,
    prune_tool_outputs,
    trim_conversation_for_context_limit,
)


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


async def fit_request(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    llm: object,
    *,
    prefix_cache_key: str | None = None,
    allow_model_compaction: bool = True,
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
    receipt = assess(candidate)

    if receipt.estimated_input_tokens < budget.compaction_trigger:
        return result(
            ContextFitStatus.COMPACTED if actions else ContextFitStatus.FIT,
            candidate,
            receipt,
            actions,
        )

    if allow_model_compaction:
        try:
            compacted = await compact_history(candidate, llm)
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
        actions.append("model_compaction_disabled")

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
    protocol_reserve = 32 + 4 * 2 + 8 * len(tools or ())
    message_budget = max(
        1,
        budget.safe_input_budget
        - estimate_tool_schema_tokens(tools)
        - protocol_reserve,
    )
    recovered = trim_conversation_for_context_limit(
        candidate,
        token_budget=message_budget,
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
