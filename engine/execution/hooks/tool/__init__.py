"""Tool-lifecycle hooks.

This subpackage owns the tool-lifecycle hook system: ``PreToolHook`` /
``PostToolHook`` / ``StopHook`` interfaces, the ``HookRegistry`` that stores
and executes them, and the ``HookLoader`` that instantiates hooks from a
``hooks.yaml`` config.

These are distinct from the engine-extension hooks in
:mod:`engine.execution.hooks.extension` (``HookManager`` / ``HookType``),
which intercept engine control points rather than tool execution.
``engine.execution.hooks`` re-exports everything for backward compatibility.
"""

from __future__ import annotations

from .interface import PostToolHook, PreToolHook, StopHook
from .loader import HookLoader
from .manager import HookRegistry

__all__ = [
    "HookLoader",
    "HookRegistry",
    "PostToolHook",
    "PreToolHook",
    "StopHook",
]
