"""Route a request through Smith's declarative identity catalog.

This module deliberately owns no domain taxonomy. Adding a legal, finance, or
other domain route is content work in ``agents/identities/*.yaml``, not a
Python edit here.
"""

from __future__ import annotations

import logging

from engine.identity_catalog import IdentityCatalog, RouteDecision
from engine.llm.observability import llm_purpose

# Backward-compatible re-exports — canonical home is engine.safety.eval_guard.
from engine.safety.eval_guard import (  # noqa: F401
    EVAL_SENSITIVE_GUIDANCE,
    detect_eval_sensitive,
)

logger = logging.getLogger(__name__)


def route_task(
    user_message: str,
    catalog: IdentityCatalog,
    *,
    identity_id: str | None = None,
) -> RouteDecision:
    """Resolve one request against the loaded identity catalog."""
    return catalog.resolve(user_message, identity_id=identity_id)


async def route_task_with_llm(
    user_message: str,
    catalog: IdentityCatalog,
    llm=None,
    *,
    identity_id: str | None = None,
) -> RouteDecision:
    """Use deterministic catalog matches first, then optionally ask an LLM.

    The LLM is constrained to route ids already declared in the catalog; it
    cannot invent a new identity or pipeline name.
    """
    deterministic = route_task(user_message, catalog, identity_id=identity_id)
    if deterministic.score > 0 or llm is None or identity_id is not None:
        return deterministic

    choices = [
        f"{identity.id}:{route.id}"
        for identity in catalog.identities
        for route in identity.routes
    ]
    if not choices:
        return deterministic
    try:
        with llm_purpose("routing"):
            response = await llm.chat([
                {
                    "role": "system",
                    "content": (
                        "Choose exactly one declared route token, or DIRECT. "
                        f"Declared routes: {', '.join(choices)}"
                    ),
                },
                {"role": "user", "content": user_message[:1000]},
            ])
        selected = response.text.strip()
        if selected.upper() == "DIRECT":
            return deterministic
        identity_key, route_key = selected.split(":", 1)
        identity = catalog.get(identity_key)
        for route in identity.routes:
            if route.id == route_key:
                return RouteDecision(identity, route.id, route.pipeline, score=1)
    except Exception:
        # LLM 路由是可选增强，失败回退确定性路由是正确行为，
        # 但必须留痕——静默吞掉会让路由质量退化永远无人发现。
        logger.warning("LLM route selection failed; falling back to deterministic route", exc_info=True)
    return deterministic
