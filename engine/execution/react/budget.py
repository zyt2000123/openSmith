from __future__ import annotations

import re

from engine.context.compression import active_turn_bounds
from engine.execution.runtime_control import (
    continue_after_length_prompt,
    incomplete_final_repair_prompt,
    tool_failure_recovery_prompt,
)

DEFAULT_MAX_REACT_ITERS = 60
MAX_FAILED_TOOL_RECOVERY_ITERS = 20
MAX_PREFLIGHT_CHALLENGE_ITERS = 20
MAX_INCOMPLETE_FINAL_REPAIRS = 2
MAX_LENGTH_CONTINUATIONS = 2
CONVERSATION_HARD_LIMIT = 40
CONVERSATION_KEEP_RECENT = 28
MAX_IDENTICAL_TOOL_ERRORS = 6
MAX_COMPACTION_FAILURES = 2
TOOL_FAILURE_HINT = tool_failure_recovery_prompt()
INCOMPLETE_FINAL_AFTER_TOOL_HINT = incomplete_final_repair_prompt()
CONTINUE_AFTER_LENGTH_HINT = continue_after_length_prompt()
TOOL_FAILURE_BUDGET_MESSAGE = (
    "Tool failure recovery budget reached before a final answer."
)
PREFLIGHT_BUDGET_MESSAGE = (
    "Tool preflight challenge budget reached before an operation could run."
)
TOOL_CALL_BUDGET_MESSAGE = (
    "Tool-call budget reached before a final answer."
)

_NEXT_ACTION_VERBS_ZH = (
    "查",
    "搜",
    "抓",
    "获取",
    "打开",
    "访问",
    "确认",
    "验证",
    "看看",
    "看一下",
)
_NEXT_ACTION_VERBS_EN = (
    "search",
    "fetch",
    "check",
    "open",
    "browse",
    "look up",
    "verify",
)
_INCOMPLETE_FINAL_PATTERNS = (
    re.compile(r"(让我|我将|我会|我需要|接下来|下一步|继续).{0,24}(" + "|".join(_NEXT_ACTION_VERBS_ZH) + r")"),
    re.compile(r"(let me|i'll|i will|i need to|next,?|going to).{0,48}(" + "|".join(_NEXT_ACTION_VERBS_EN) + r")"),
)


def looks_like_incomplete_final_after_tool(text: str) -> bool:
    """Return true when a supposed final answer is only a promise to keep acting."""
    normalized = " ".join(text.strip().split()).lower()
    if not normalized or len(normalized) > 240:
        return False
    return any(pattern.search(normalized) for pattern in _INCOMPLETE_FINAL_PATTERNS)


def trim_conversation_to_message_cap(conversation: list[dict]) -> list[dict]:
    """Bound the conversation by message count without losing the request.

    Keeps the leading contract, the newest real user turn, and a recent tail.

    A fixed ``conversation[:2]`` head only holds when the run opens with
    ``[system, request]``.  The real layout is ``[system, *session history,
    request]`` (agent_loop), so with any history index 1 is the *oldest* history
    message and the request sits near the end: once tool traffic pushed it left
    out of the tail window this cap silently deleted the instruction being
    carried out, and the loop kept calling tools against a days-old user turn.

    Lives here rather than inline in the loop because the regression tests must
    exercise the same code the loop runs — a test-local copy of this algorithm
    kept passing while production drifted away from it.
    """
    if len(conversation) <= CONVERSATION_HARD_LIMIT:
        return conversation

    leading_system_count, active_start = active_turn_bounds(conversation)

    def aligned(cut: int) -> int:
        """Move *cut* forward so the tail opens on a user turn when it must.

        Anthropic refuses a conversation whose first non-system message is not
        ``user`` (adapters/anthropic.py), and that refusal is not a
        context-limit error, so it fails the whole run instead of triggering a
        retry.  The old fixed ``conversation[:2]`` head satisfied this by
        accident -- index 1 of [system, *history, request] is a history *user*
        turn.  Selecting the head by role removed that accident: the boundary
        scan below stops on ``assistant`` quite happily, so a session sitting at
        the 40-message history limit produced [system, assistant, ...] and every
        turn of that session failed on its first provider call.
        """
        if active_start is None or active_start < cut:
            # 请求会被钉进 head（见 kept），首条非 system 就是它。
            return cut
        while cut < active_start and conversation[cut].get("role") != "user":
            cut += 1
        return cut

    def kept(cut: int) -> list[dict]:
        head = conversation[:leading_system_count]
        if active_start is not None and active_start < cut:
            # 请求落在被丢弃的中段：钉进 head。它之后的工作照常可裁 ——
            # 保住的是指令本身，不是整条活动轮。
            head = [*head, conversation[active_start]]
        return head + conversation[cut:]

    # 切点落在 tool 结果串中会拆散 assistant(tool_calls)/tool 配对
    # （provider 400）。向前回退到 assistant/user 边界：同一轮的 tool
    # 结果之间可能夹着 system 提示（TOOL_FAILURE_HINT 在 tool_calls
    # 循环内 append），只认 role=="tool" 会在提示处停下留下孤儿。
    requested_cut = len(conversation) - CONVERSATION_KEEP_RECENT
    cut = requested_cut
    while cut > leading_system_count and conversation[cut].get("role") in ("tool", "system"):
        cut -= 1
    trimmed = kept(aligned(cut))
    if len(trimmed) < len(conversation):
        return trimmed

    # 回退吃光了可丢弃的中段，head+tail 就是整条对话，这道上限静默失效：
    # 一轮 8 个并行工具调用即可（实测 41 条裁剪后仍是 41 条，30 个调用时
    # 63 条原样返回）。改为向后找下一个边界 —— 丢弃的更多，但配对完整且
    # 一定有进展。判据用"结果是否真的变短"而不是切点下标：head 的长度随
    # 请求是否被钉入而变，下标比较会漏判。
    forward = requested_cut
    while forward < len(conversation) and conversation[forward].get("role") in ("tool", "system"):
        forward += 1
    if forward < len(conversation):
        forward_trimmed = kept(aligned(forward))
        if len(forward_trimmed) < len(conversation):
            return forward_trimmed
    # 整条尾巴都是 tool/system 时没有安全切点，保持原样交给 fit_request 兜底。
    return conversation


def budget_exhausted_message(reason: str) -> str:
    return (
        f"{reason} I stopped to avoid an infinite loop. "
        "Please retry with a narrower request or inspect the latest failed tool result."
    )
