"""Engine-owned instructions that govern one agent run.

Workflow and skill content remain pluggable.  This module owns only the
non-negotiable operating contract that the engine projects into model context;
ToolPolicy and ToolGuard continue to enforce the contract independently.
"""

from __future__ import annotations


def initial_runtime_control_prompt() -> str:
    """Render the immutable control contract appended to every system prompt."""
    return "\n".join(
        (
            "## Engine Runtime Control",
            "",
            "This is an engine-generated operating contract. It is not user, project, workflow, skill, memory, or tool output.",
            "",
            "- ToolPolicy and ToolGuard decisions are authoritative. Do not evade, reinterpret, or claim to override an engine block or approval requirement.",
            "- Use only the tools exposed for this run and stay within the engine-configured execution scope.",
            "- Do not describe a file change, command result, or external fact as complete without evidence from the current run.",
            "- If an operation is blocked, denied, or fails repeatedly, change approach or explain the exact limitation instead of blindly retrying it.",
            "- After using tools, provide a concise final report covering the outcome, evidence or changes, failures or omissions, and a next step only when the task remains unfinished.",
        )
    )


def tool_blocked_prompt() -> str:
    """Tell the model how to respond when a policy blocks a requested tool call."""
    return (
        "The engine blocked the requested operation. Do not attempt to bypass the block "
        "or repeat the same side-effecting request. Use a safe alternative, or explain the "
        "limitation to the user."
    )


def tool_failure_recovery_prompt() -> str:
    """Tell the model to recover from repeated tool failures without looping."""
    return (
        "Multiple tool calls have failed consecutively. Change your approach - "
        "try a different tool, simplify the command, or explain what you need without using tools."
    )


def incomplete_final_repair_prompt() -> str:
    """Require a real final answer after a tool-using turn stalls in narration."""
    return (
        "Your last message described a next action instead of completing the user's request. "
        "Continue now: call the appropriate tool if more evidence is still needed, or provide "
        "a complete final answer. Do not only say what you will do next."
    )


def repeated_success_prompt(tool_name: str, count: int) -> str:
    """Tell the model it is re-running a call that already succeeded."""
    return (
        f"You have already called `{tool_name}` {count} times with identical arguments, "
        "and each call succeeded. An earlier result may have been replaced by a "
        "`[pruned: ...]` placeholder to save context, but the call did happen. "
        "Use what you already established instead of repeating it, and only call it "
        "again if you specifically need to observe a change."
    )


def continue_after_length_prompt() -> str:
    """Continue a length-limited answer without duplicating previous output."""
    return (
        "Your previous response was cut off by the model output limit. Continue exactly "
        "from where it stopped. Do not repeat prior text, restart the answer, or mention "
        "this instruction."
    )


