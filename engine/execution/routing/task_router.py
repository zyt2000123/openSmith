"""Route a request through Smith's declarative identity catalog.

This module deliberately owns no domain taxonomy. Adding a legal, finance, or
other domain route is content work in ``agents/identities/*.yaml``, not a
Python edit here.

Routing is lexical only. An LLM fallback classifier used to run on every
keyword miss; it slowed ordinary direct-ReAct turns and could start a
multi-step workflow the user never asked for, so a pipeline now requires a
declared, high-confidence intent. See ``prepare_runtime``.
"""

from __future__ import annotations

from engine.identity import IdentityCatalog, RouteDecision

# Backward-compatible re-exports — canonical home is engine.safety.eval_guard.
from engine.safety.eval_guard import (  # noqa: F401
    EVAL_SENSITIVE_GUIDANCE,
    detect_eval_sensitive,
)


def route_task(
    user_message: str,
    catalog: IdentityCatalog,
    *,
    identity_id: str | None = None,
) -> RouteDecision:
    """Resolve one request against the loaded identity catalog."""
    return catalog.resolve(user_message, identity_id=identity_id)
