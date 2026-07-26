from __future__ import annotations

from engine.execution.runtime_control import (
    continue_after_length_prompt,
    finalize_without_tools_prompt,
    incomplete_final_repair_prompt,
    initial_runtime_control_prompt,
    tool_blocked_prompt,
    tool_failure_recovery_prompt,
)


def test_initial_runtime_control_keeps_engine_authority_and_delivery_contract() -> None:
    prompt = initial_runtime_control_prompt()

    assert prompt.startswith("## Engine Runtime Control")
    assert "engine-generated" in prompt
    assert "ToolPolicy" in prompt
    assert "ToolGuard" in prompt
    assert "final report" in prompt


def test_runtime_control_directives_cover_block_failure_and_finalization() -> None:
    assert "Do not attempt to bypass" in tool_blocked_prompt()
    assert "Change your approach" in tool_failure_recovery_prompt()
    assert "complete final answer" in incomplete_final_repair_prompt()
    assert "Do not repeat prior text" in continue_after_length_prompt()

    finalization = finalize_without_tools_prompt("tool budget exhausted")
    assert finalization.startswith("tool budget exhausted")
    assert "what was completed" in finalization
    assert "what failed" in finalization
