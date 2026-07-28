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


CONDITIONS = {
    "coding_needs_architecture": coding_needs_architecture,
}
