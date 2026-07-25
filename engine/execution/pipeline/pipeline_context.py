"""Documented key namespace for the pipeline execution context dict.

The pipeline context is a plain ``dict`` shared by nodes, gates, conditions,
and checkpointing.  All engine code and gate/condition content files must
reference keys through these constants (or :func:`output_key`) instead of
raw string literals, so a typo fails at import time rather than silently
reading an empty value.

Keys prefixed with ``_`` are engine-internal and are excluded from session
checkpoints (see pipeline._save_checkpoint).
"""

from __future__ import annotations

# --- request / routing (written once by agent_loop) -----------------------
CTX_USER_MESSAGE = "user_message"
CTX_IDENTITY_ID = "identity_id"
CTX_ROUTE_ID = "route_id"
CTX_AGENT_ID = "agent_id"
CTX_SESSION_ID = "session_id"
CTX_TASK_TYPE = "task_type"
CTX_FORCED_SKILL = "forced_skill"

# --- engine-internal (never checkpointed) ----------------------------------
CTX_STATE_DIR = "_state_dir"
CTX_WORKING_DIR = "_working_dir"
CTX_RETRY_HINT = "_rubric_retry_hint"

# --- skill-facing feedback --------------------------------------------------
CTX_RUBRIC_FEEDBACK = "rubric_feedback"


def output_key(skill_name: str) -> str:
    """Context key holding a committed pipeline node's output text."""
    return f"{skill_name}_output"
