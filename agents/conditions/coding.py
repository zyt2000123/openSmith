"""Coding-domain step conditions.

Content layer: loaded by ``engine.execution.skill_chain.load_gate_content``.
The module-level ``CONDITIONS`` mapping is merged into the condition
registry that pipeline YAML ``condition:`` keys resolve against.
"""

from __future__ import annotations

import re


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
    "coding_bugfix_needs_diagnosis": coding_bugfix_needs_diagnosis,
}
