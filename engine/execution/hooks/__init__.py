"""Hook system.

Single import path ``engine.execution.hooks`` for two hook systems:

- Tool lifecycle hooks (:mod:`engine.execution.hooks.tool`):
  ``PreToolHook`` / ``PostToolHook`` / ``StopHook`` + ``HookRegistry`` +
  ``HookLoader`` — intercept around tool execution.
- Engine-extension hooks (:mod:`engine.execution.hooks.extension`):
  ``HookManager`` / ``HookType`` — rewrite prompts, memory lifecycle ticks,
  and after-turn persistence.

Both systems are re-exported here so callers keep one import path.
"""

from __future__ import annotations

from .extension import DEFAULT_HOOK_TIMEOUT_SECONDS, HookManager, HookType
from .tool import HookLoader, HookRegistry, PostToolHook, PreToolHook, StopHook

__all__ = [
    "PreToolHook",
    "PostToolHook",
    "StopHook",
    "HookRegistry",
    "HookLoader",
    "HookManager",
    "HookType",
    "DEFAULT_HOOK_TIMEOUT_SECONDS",
]
