from __future__ import annotations

import re

from engine.execution.runtime_control import (
    continue_after_length_prompt,
    incomplete_final_repair_prompt,
    tool_failure_recovery_prompt,
)

DEFAULT_MAX_REACT_ITERS = 60
MAX_FAILED_TOOL_RECOVERY_ITERS = 20
MAX_PREFLIGHT_CHALLENGE_ITERS = 20
MAX_INCOMPLETE_FINAL_REPAIRS = 2
# How much text may still count as "only a promise".  The cap stands in for
# "delivered nothing", so it has to be generous enough to cover a promise that
# comes with a paragraph of preamble -- 240 chars is barely two sentences of
# English.  It cannot go much higher without catching real answers that close
# with a suggestion ("接下来你可以自己跑一下测试"), and that misfire costs one
# extra repair round, where a miss costs the user an unanswered question.
INCOMPLETE_FINAL_MAX_CHARS = 400
MAX_LENGTH_CONTINUATIONS = 2
CONVERSATION_HARD_LIMIT = 40
CONVERSATION_KEEP_RECENT = 28
CONVERSATION_KEEP_HEAD = 2
MAX_IDENTICAL_TOOL_ERRORS = 6
# Identical *successful* calls had no ceiling at all: identical_error_count only
# counts failures and is reset by every success, so a tool could be re-run with
# the same arguments until max_iters ran out.  Warn rather than block -- a file
# may legitimately have changed between two reads.
REPEATED_SUCCESS_WARN_THRESHOLD = 3
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
    if not normalized or len(normalized) > INCOMPLETE_FINAL_MAX_CHARS:
        return False
    return any(pattern.search(normalized) for pattern in _INCOMPLETE_FINAL_PATTERNS)


def budget_exhausted_message(reason: str) -> str:
    return (
        f"{reason} I stopped to avoid an infinite loop. "
        "Please retry with a narrower request or inspect the latest failed tool result."
    )
