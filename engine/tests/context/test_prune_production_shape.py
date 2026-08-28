"""Pruning has to work on the message shape the engine actually produces.

Tool results only ever exist *after* the last user message: ``react_loop``
appends ``role:"tool"`` but never ``role:"user"``, and session history cannot
supply them either — ``messages.role`` is ``CHECK (role IN
('user','assistant','system'))``.  Counting user turns backwards therefore
protected every tool result ever written, so ``prune_tool_outputs`` returned 0
on every call and a tool-heavy turn died with ``context_capacity_exhausted``
instead of being shrunk.

The pre-existing coverage builds ``[system, user, tool, tool, tool, user,
user]`` — two trailing user turns after the tool trail — which the engine
cannot produce, so it exercised only the protected branch.
"""

from __future__ import annotations

import asyncio

from engine.context import ContextFitStatus, fit_request
from engine.context.compression import (
    PRUNE_PROTECT_THRESHOLD_CHARS,
    prune_tool_outputs,
)
from engine.llm.contracts import ModelLimits


class _BoundedLLM:
    limits = ModelLimits(
        context_window=8_192,
        context_window_declared=True,
        max_output_tokens=2_048,
        max_output_tokens_declared=True,
    )


def _agentic_turn(tool_count: int, chunk: int = 9_000) -> list[dict]:
    """One user request followed by a long tool trail — the production shape."""
    conversation: list[dict] = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "please audit this repository"},
    ]
    for index in range(tool_count):
        conversation.append({"role": "assistant", "content": f"calling tool {index}"})
        conversation.append({"role": "tool", "content": f"result-{index}-" + "X" * chunk})
    return conversation


def test_prunes_a_long_tool_trail_in_the_current_turn() -> None:
    conversation = _agentic_turn(tool_count=8)

    pruned = prune_tool_outputs(conversation)

    assert pruned > 0, "a long tool trail in the current turn must be prunable"


def test_keeps_the_most_recent_tool_output() -> None:
    """Protection comes from the char threshold, not from counting user turns."""
    conversation = _agentic_turn(tool_count=8)
    newest_before = conversation[-1]["content"]

    prune_tool_outputs(conversation)

    assert conversation[-1]["content"] == newest_before, "the newest result was pruned"


def test_protects_roughly_the_threshold_of_recent_output() -> None:
    conversation = _agentic_turn(tool_count=8)

    prune_tool_outputs(conversation)

    kept = sum(
        len(m["content"])
        for m in conversation
        if m.get("role") == "tool" and not m["content"].startswith("[pruned")
    )
    assert kept >= PRUNE_PROTECT_THRESHOLD_CHARS, (
        f"pruning kept only {kept} chars of recent tool output"
    )


def test_a_short_tool_trail_is_left_alone() -> None:
    """Below the protection threshold there is nothing worth pruning."""
    conversation = _agentic_turn(tool_count=1, chunk=500)

    assert prune_tool_outputs(conversation) == 0


def test_tool_heavy_turn_no_longer_fails_to_fit() -> None:
    """End state: the run survives instead of aborting on capacity."""
    conversation = _agentic_turn(tool_count=8)

    result = asyncio.run(fit_request(conversation, None, _BoundedLLM()))

    assert result.status is not ContextFitStatus.UNFIT_REQUEST, (
        f"a tool-heavy turn still cannot be fitted: {result.actions}"
    )


def test_a_pruned_result_still_proves_the_call_happened() -> None:
    """The stub must not erase the evidence that the tool already ran.

    Regression: pruning replaced the payload with a bare "[pruned]", leaving the
    model no sign of what that call produced -- or that it succeeded at all.
    Re-running the tool was then the rational move, which burned the iteration
    budget and produced the duplicate-call behaviour this guards against.
    """
    conversation: list[dict] = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "tool", "content": "A" * 9000},
        {"role": "tool", "content": "B" * 9000},
    ]

    assert prune_tool_outputs(conversation) > 0

    stub = conversation[2]["content"]
    assert stub != "[pruned]", "a bare marker carries no evidence"
    assert "9000" in stub, "the stub must say how much output was dropped"
    assert conversation[3]["content"] == "B" * 9000, "the newest result stays intact"
