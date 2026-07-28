"""Context compression — prune old tool outputs + LLM-based compaction."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from engine.llm.contracts import DEFAULT_CONTEXT_WINDOW
from .budget import (
    CONTEXT_COMPACTION_INPUT_RATIO,
    CONTEXT_COMPACTION_TRIGGER,
    CONTEXT_SAFETY_MARGIN_RATIO,
    context_budget_for,
    estimate_compressible_tokens,
    estimate_messages_tokens,
    estimate_tokens,
    model_limits_for,
)
from .summary import summarize_session

if TYPE_CHECKING:
    from engine.llm.port import LLMPort

logger = logging.getLogger(__name__)

_PRUNED_MARKER = "[pruned]"
PRUNE_PROTECT_TURNS = 2
PRUNE_PROTECT_THRESHOLD_CHARS = 8000
PRUNE_MIN_CHARS = 2000
CONTEXT_TRIGGER_RATIO = 0.7
DEFAULT_CONTEXT_LIMIT = DEFAULT_CONTEXT_WINDOW
CONTEXT_DISPLAY_WINDOW = DEFAULT_CONTEXT_LIMIT

def prune_tool_outputs(
    conversation: list[dict],
    *,
    protect_turns: int = PRUNE_PROTECT_TURNS,
    protect_threshold: int = PRUNE_PROTECT_THRESHOLD_CHARS,
    min_prune: int = PRUNE_MIN_CHARS,
) -> int:
    """Remove old tool outputs in-place, protecting recent turns.

    Returns number of chars pruned.
    """
    turns = 0
    total_chars = 0
    pruned_chars = 0
    to_prune: list[dict] = []

    for i in range(len(conversation) - 1, -1, -1):
        msg = conversation[i]
        if msg.get("role") == "user":
            turns += 1
        if msg.get("role") == "tool":
            if turns < protect_turns:
                continue
            content = msg.get("content", "")
            # Exact match, not substring: a real tool output that merely
            # mentions the marker (grepping this repo does) would otherwise
            # stop pruning early.
            if content == _PRUNED_MARKER:
                break
            char_count = len(content) if isinstance(content, str) else 0
            total_chars += char_count
            if total_chars > protect_threshold:
                to_prune.append(msg)
                pruned_chars += char_count

    if pruned_chars < min_prune:
        return 0

    for msg in to_prune:
        msg["content"] = _PRUNED_MARKER

    return pruned_chars


def _conversation_tokens(conversation: list[dict]) -> int:
    return estimate_compressible_tokens(conversation)


def needs_compaction(
    conversation: list[dict],
    context_limit: int = DEFAULT_CONTEXT_LIMIT,
    *,
    trigger_ratio: float = CONTEXT_TRIGGER_RATIO,
) -> bool:
    return _conversation_tokens(conversation) >= context_limit * trigger_ratio


def context_limit_for_llm(llm: object | None) -> int:
    """Use the selected route's declared model window with a safe fallback."""
    return model_limits_for(llm).context_window


def compaction_policy_for_llm(llm: object | None) -> tuple[int, float]:
    """Return a safe input budget and the selected compaction trigger.

    The shell displays context against a stable 256k reference window. To
    keep the actual request from growing beyond the commonly supported 128k
    range, reserve room for output and provider/tool protocol overhead before
    deciding when to compact. A smaller declared window remains the hard
    safety limit for that route.
    """
    budget = context_budget_for(model_limits_for(llm))
    return budget.safe_input_budget, CONTEXT_COMPACTION_INPUT_RATIO


def prompt_budget_for_llm(llm: object | None) -> int:
    """Limit static prompt assembly to leave room for conversation history."""
    input_budget, _ = compaction_policy_for_llm(llm)
    return max(1, int(input_budget * 0.6))


def trim_conversation_for_context_limit(
    conversation: list[dict],
    *,
    token_budget: int,
) -> list[dict]:
    """Deterministically shrink an over-limit conversation without another LLM call.

    This recovery path deliberately removes tool-call structure instead of
    retaining an orphaned assistant/tool pair. It is only used after a provider
    explicitly rejects context length, before any tool from the current model
    turn has been executed.
    """
    copied = [dict(message) for message in conversation]
    if token_budget <= 0 or estimate_messages_tokens(copied) <= token_budget:
        return copied

    # The first system message is the runtime contract. Never silently rewrite
    # it to make a request fit; callers must classify an oversized contract as
    # an explicit unfit-static-prompt result.
    protected: list[dict] = []
    history_start = 0
    while (
        history_start < len(copied)
        and copied[history_start].get("role") == "system"
    ):
        protected.append(copied[history_start])
        history_start += 1

    history_lines: list[str] = []
    for message in copied[history_start:]:
        role = str(message.get("role", "unknown"))
        content = message.get("content")
        if not isinstance(content, str) or not content:
            tool_calls = message.get("tool_calls")
            if tool_calls:
                content = json.dumps(
                    tool_calls,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
            else:
                content = ""
        if content:
            history_lines.append(f"[{role}] {content}")

    recovery_prefix = (
        "[Context deterministically shortened to fit the selected model]\n"
    )
    history_text = "\n".join(history_lines)

    def candidate(keep: int) -> list[dict]:
        tail = history_text[-keep:] if keep else ""
        marker = "[... earlier context truncated ...]\n" if keep < len(history_text) else ""
        return [
            *protected,
            {"role": "user", "content": recovery_prefix + marker + tail},
        ]

    minimum = candidate(0)
    if estimate_messages_tokens(minimum) > token_budget:
        # Return the protected contract unchanged. The final fitter will fail
        # closed instead of corrupting it or calling the provider.
        return protected

    low, high = 0, len(history_text)
    best = minimum
    while low <= high:
        keep = (low + high) // 2
        current = candidate(keep)
        if estimate_messages_tokens(current) <= token_budget:
            best = current
            low = keep + 1
        else:
            high = keep - 1
    return best


def _trim_middle(text: str, token_budget: int) -> str:
    if token_budget <= 0:
        return ""
    if estimate_tokens(text) <= token_budget:
        return text
    marker = "\n[... context truncated ...]\n"
    low, high = 0, len(text)
    best = ""
    while low <= high:
        keep = (low + high) // 2
        head = keep // 2
        tail = keep - head
        candidate = text[:head] + marker + (text[-tail:] if tail else "")
        if estimate_tokens(candidate) <= token_budget:
            best = candidate
            low = keep + 1
        else:
            high = keep - 1
    return best or _trim_tail(marker, token_budget)


def _trim_tail(text: str, token_budget: int) -> str:
    if token_budget <= 0:
        return ""
    if estimate_tokens(text) <= token_budget:
        return text
    marker = "[... earlier context truncated ...]\n"
    low, high = 0, len(text)
    best = ""
    while low <= high:
        keep = (low + high) // 2
        candidate = marker + (text[-keep:] if keep else "")
        if estimate_tokens(candidate) <= token_budget:
            best = candidate
            low = keep + 1
        else:
            high = keep - 1
    return best


async def compress(conversation: list[dict], llm: "LLMPort | None" = None) -> list[dict]:
    """Two-stage compression: prune first, compact if still over threshold.

    Returns the conversation list (mutated in-place for prune, replaced for compact).
    """
    prune_tool_outputs(conversation)
    if llm is not None:
        context_limit, trigger_ratio = compaction_policy_for_llm(llm)
        if needs_compaction(
            conversation,
            context_limit=context_limit,
            trigger_ratio=trigger_ratio,
        ):
            return await compact_history(conversation, llm)
    return conversation


async def compact_history(conversation: list[dict], llm: "LLMPort") -> list[dict]:
    """Replace conversation with a compacted summary via LLM.

    Returns a new conversation list: [system_prompt, summary_message].
    The original system prompt (first message) is preserved.
    """
    system_messages: list[dict] = []
    for message in conversation:
        if message.get("role") != "system":
            break
        system_messages.append(message)

    summary_result = await summarize_session(conversation, llm)
    if not summary_result.usable:
        # 摘要为空/被截断/被拒答时整体替换历史等于静默失忆——
        # 放弃本轮 compact，保留 prune 后的原始对话。
        logger.warning(
            "compact_history discarded (status=%s, finish_reason=%r, "
            "summary_chars=%d); keeping original conversation",
            summary_result.status.value,
            summary_result.finish_reason,
            len(summary_result.summary),
        )
        return conversation

    result = []
    result.extend(system_messages)
    result.append({
        "role": "user",
        "content": f"[Previous conversation summary]\n{summary_result.summary}",
    })
    result.append({"role": "assistant", "content": "Understood. I have the full context from our previous conversation. How can I help?"})
    return result
