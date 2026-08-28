"""Sub-agent tool — delegate scoped work to isolated agents, get summaries back.

The engine injects the spawn capability (``_spawn``) and the available type
catalog (``_agent_types``); this module owns only argument validation and the
rendering of the aggregate report the parent model reads.
"""

import json

TOOL_META = {
    "name": "sub_agent",
    "description": (
        "Delegate one or more scoped tasks to isolated sub-agents that run in "
        "parallel and report back. Each sub-agent starts with a FRESH context: "
        "it sees only the prompt you write, never this conversation. You get "
        "back only its final summary, not its tool calls.\n"
        "Use it to (a) fan out independent investigations, (b) keep a large, "
        "noisy search out of this conversation, (c) get an independent second "
        "opinion. Do NOT use it for work that needs this conversation's "
        "context, for a single trivial lookup, or for anything requiring "
        "user interaction — a sub-agent cannot ask questions.\n"
        "Write each prompt as a standalone brief: state the goal, the "
        "constraints, and exactly what to report.\n"
        "Parallel tasks must not touch the same files: concurrent edits are "
        "last-write-wins and nothing detects the clash. Give each task a "
        "disjoint scope, or run them in separate calls."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "description": "One entry per sub-agent. Independent tasks run concurrently.",
                "items": {
                    "type": "object",
                    "properties": {
                        "agent_type": {
                            "type": "string",
                            "description": "Which sub-agent type to spawn.",
                        },
                        "prompt": {
                            "type": "string",
                            "description": (
                                "Self-contained brief. The sub-agent has no access to "
                                "this conversation, so restate every fact it needs."
                            ),
                        },
                        "label": {
                            "type": "string",
                            "description": "Short name for this task, shown in the report.",
                        },
                    },
                    "required": ["agent_type", "prompt"],
                },
            },
            "max_parallel": {
                "type": "integer",
                "description": "How many sub-agents may run at once (default 4).",
            },
        },
        "required": ["tasks"],
    },
    "permission_level": "execute",
    "approval_policy": "policy",
    # A sub-agent may itself hold write tools, so the outer call is not a read.
    "side_effect": "write",
    # One fan-out at a time: the width lives inside this call, not across calls.
    "concurrency": "serial",
    "execution_environment": "host",
}

# The runtime truncates any tool result past 50 KB and spills the rest to a
# file, so a report that overruns loses its *tail* — the last sub-agents'
# findings vanish from the parent's view entirely. Budget the whole report
# under that ceiling and split it across however many agents ran.
REPORT_BYTE_BUDGET = 40 * 1024
# Even one agent gets a ceiling: an 8k-token summary defeats the point of
# delegating to save context.
MAX_SUMMARY_BYTES = 12 * 1024
MIN_SUMMARY_BYTES = 1024


def _clip(text, budget_bytes):
    """Trim *text* to *budget_bytes* UTF-8 bytes without splitting a character.

    Bytes, not characters: a CJK report is ~3x its character count, and a
    character-based cap would sail past the runtime's byte ceiling.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= budget_bytes:
        return text
    clipped = encoded[:budget_bytes].decode("utf-8", errors="ignore")
    return clipped + f"\n…[truncated to {budget_bytes} bytes]"


def _coerce_tasks(tasks, known_types):
    """Validate the model's task list, returning (tasks, error_message)."""
    if isinstance(tasks, str):
        # Some providers hand back a JSON-encoded array for nested schemas.
        try:
            tasks = json.loads(tasks)
        except ValueError:
            return None, "tasks must be an array of objects"
    if not isinstance(tasks, list) or not tasks:
        return None, "tasks must be a non-empty array"

    cleaned = []
    for index, raw in enumerate(tasks):
        if not isinstance(raw, dict):
            return None, f"tasks[{index}] must be an object"
        agent_type = raw.get("agent_type")
        prompt = raw.get("prompt")
        if not isinstance(agent_type, str) or not agent_type.strip():
            return None, f"tasks[{index}].agent_type must be a non-empty string"
        if not isinstance(prompt, str) or not prompt.strip():
            return None, f"tasks[{index}].prompt must be a non-empty string"
        agent_type = agent_type.strip()
        if known_types and agent_type not in known_types:
            return None, (
                f"tasks[{index}].agent_type {agent_type!r} is unknown; "
                f"available: {', '.join(known_types)}"
            )
        label = raw.get("label")
        cleaned.append(
            {
                "agent_type": agent_type,
                "prompt": prompt.strip(),
                "label": label.strip() if isinstance(label, str) else "",
            }
        )
    return cleaned, ""


def _render(outcomes):
    """Render outcomes as the single text block the parent model reads."""
    per_agent = max(
        MIN_SUMMARY_BYTES,
        min(MAX_SUMMARY_BYTES, REPORT_BYTE_BUDGET // max(1, len(outcomes))),
    )
    lines = []
    succeeded = sum(1 for item in outcomes if item.get("ok"))
    lines.append(f"# Sub-agent report ({succeeded}/{len(outcomes)} succeeded)")
    for item in outcomes:
        label = item.get("label") or item.get("agent_type") or "sub-agent"
        status = "OK" if item.get("ok") else "FAILED"
        header = f"\n## [{status}] {label} ({item.get('agent_type', '?')})"
        stats = []
        if item.get("tool_calls"):
            stats.append(f"{item['tool_calls']} tool calls")
        usage = item.get("usage") or {}
        if usage.get("total_tokens"):
            stats.append(f"{usage['total_tokens']} tokens")
        if stats:
            header += f" — {', '.join(stats)}"
        lines.append(header)
        if item.get("error"):
            lines.append(f"Error: {item['error']}")
        summary = (item.get("summary") or "").strip()
        if summary:
            lines.append(_clip(summary, per_agent))
        elif not item.get("error"):
            lines.append("(no report returned)")
    return "\n".join(lines)


async def execute(tasks=None, max_parallel=4, _spawn=None, _agent_types=(), **_extra):
    if _spawn is None:
        return "Error: sub-agent execution is not available in this runtime"
    if not _agent_types:
        return "Error: no sub-agent types are installed"

    cleaned, problem = _coerce_tasks(tasks, tuple(_agent_types))
    if problem:
        return f"Error: {problem}"

    if isinstance(max_parallel, bool) or not isinstance(max_parallel, int):
        max_parallel = 4

    try:
        outcomes = await _spawn(cleaned, max_parallel)
    except ValueError as exc:
        return f"Error: {exc}"
    return _render(outcomes)
