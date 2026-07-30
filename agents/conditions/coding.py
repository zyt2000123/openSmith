"""Coding-domain step conditions.

Content layer: loaded by ``engine.execution.skill_chain.load_gate_content``.
The module-level ``CONDITIONS`` mapping is merged into the condition
registry that pipeline YAML ``condition:`` keys resolve against.
"""

from __future__ import annotations

import re


def coding_needs_architecture(ctx: dict) -> bool:
    """Skip architecture for small, single-module changes."""
    # ``load_gate_content`` injects ``output_key`` before this content module
    # executes.  Keeping that dependency injected avoids importing engine code.
    plan_output = ctx.get(output_key("coding-planning"), "")  # noqa: F821
    file_refs = re.findall(r'[\w/]+\.\w{1,5}', plan_output)
    return len(set(file_refs)) >= 3


def coding_bugfix_needs_diagnosis(ctx: dict) -> bool:
    """Run the upstream diagnosis loop only for an explicit bug-fix task.

    The diagnosing-bugs skill first reuses a supplied RED loop when one exists;
    otherwise it builds the missing reproducible loop before TDD implementation.
    Feature work skips this node and starts by writing its own failing test.
    """
    request = str(ctx.get("chain_request") or ctx.get("user_message") or "")
    return bool(re.search(
        r"\b(?:bug|fix|debug|regression|crash|error|broken|failure)\b|"
        r"修复|缺陷|报错|异常|崩溃|回归",
        request,
        re.IGNORECASE,
    ))


CONDITIONS = {
    "coding_needs_architecture": coding_needs_architecture,
    "coding_bugfix_needs_diagnosis": coding_bugfix_needs_diagnosis,
}
