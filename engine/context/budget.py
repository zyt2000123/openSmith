"""One conservative request-cost model for every context decision."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from engine.llm.contracts import (
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_MAX_OUTPUT_TOKENS,
    ModelLimits,
)


CONTEXT_COMPACTION_TRIGGER = 128_000
CONTEXT_SAFETY_MARGIN_RATIO = 0.10
CONTEXT_COMPACTION_INPUT_RATIO = 0.85


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """Capacity available to one complete provider request."""

    model_context_window: int
    effective_context_window: int
    output_reserve: int
    safety_margin: int
    safe_input_budget: int
    compaction_trigger: int
    window_declared: bool
    output_limit_declared: bool


def estimate_tokens(text: str) -> int:
    """Conservatively estimate mixed CJK and non-CJK text.

    A CJK ideograph is three UTF-8 bytes, which is the stable fallback upper
    bound when a provider tokenizer has no merged token for that character.
    """
    if not text:
        return 0
    cjk = sum(1 for char in text if "一" <= char <= "鿿")
    return (3 * cjk) + (len(text) - cjk + 2) // 3


def _estimate_json(value: Any) -> int:
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
    except (TypeError, ValueError):
        serialized = repr(value)
    return estimate_tokens(serialized)


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Count the complete message objects, including tool calls and arguments."""
    return sum(_estimate_json(message) + 4 for message in messages)


def estimate_compressible_tokens(messages: list[dict[str, Any]]) -> int:
    """Count payload that history compaction can materially reduce.

    Provider-envelope overhead belongs to final request fitting, not to the
    threshold that decides when conversation history itself should compact.
    Tool-call arguments remain included because compaction can remove them.
    """
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        tool_calls = message.get("tool_calls")
        if tool_calls:
            total += _estimate_json(tool_calls)
    return total


def estimate_tool_schema_tokens(tools: list[dict[str, Any]] | None) -> int:
    """Count provider-visible tool schemas, not only prompt descriptions."""
    if not tools:
        return 0
    return _estimate_json(tools) + 8 * len(tools)


def model_limits_for(llm: object | None) -> ModelLimits:
    """Read the typed Interface with a compatibility fallback for test adapters."""
    limits = getattr(llm, "limits", None)
    if isinstance(limits, ModelLimits):
        return limits

    context_window = getattr(llm, "context_window", None)
    if (
        isinstance(context_window, bool)
        or not isinstance(context_window, int)
        or context_window <= 0
    ):
        context_window = DEFAULT_CONTEXT_WINDOW
        context_declared = False
    else:
        context_declared = bool(getattr(llm, "context_window_declared", True))

    max_output_tokens = getattr(llm, "max_output_tokens", None)
    if (
        isinstance(max_output_tokens, bool)
        or not isinstance(max_output_tokens, int)
        or max_output_tokens <= 0
    ):
        max_output_tokens = DEFAULT_MAX_OUTPUT_TOKENS
        output_declared = False
    else:
        output_declared = bool(getattr(llm, "max_output_tokens_declared", True))

    return ModelLimits(
        context_window=context_window,
        context_window_declared=context_declared,
        max_output_tokens=max_output_tokens,
        max_output_tokens_declared=output_declared,
    )


def context_budget_for(limits: ModelLimits) -> ContextBudget:
    effective_window = min(
        max(1, limits.context_window),
        CONTEXT_COMPACTION_TRIGGER,
    )
    output_reserve = min(
        max(1, limits.max_output_tokens),
        max(effective_window - 1, 1),
    )
    safety_margin = min(
        max(256, int(effective_window * CONTEXT_SAFETY_MARGIN_RATIO)),
        max(effective_window - output_reserve - 1, 0),
    )
    safe_input_budget = max(
        1,
        effective_window - output_reserve - safety_margin,
    )
    return ContextBudget(
        model_context_window=limits.context_window,
        effective_context_window=effective_window,
        output_reserve=output_reserve,
        safety_margin=safety_margin,
        safe_input_budget=safe_input_budget,
        compaction_trigger=max(
            1,
            int(safe_input_budget * CONTEXT_COMPACTION_INPUT_RATIO),
        ),
        window_declared=limits.context_window_declared,
        output_limit_declared=limits.max_output_tokens_declared,
    )


__all__ = (
    "CONTEXT_COMPACTION_INPUT_RATIO",
    "CONTEXT_COMPACTION_TRIGGER",
    "CONTEXT_SAFETY_MARGIN_RATIO",
    "ContextBudget",
    "context_budget_for",
    "estimate_compressible_tokens",
    "estimate_messages_tokens",
    "estimate_tokens",
    "estimate_tool_schema_tokens",
    "model_limits_for",
)
