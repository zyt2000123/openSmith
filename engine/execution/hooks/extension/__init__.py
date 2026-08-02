"""Engine-extension hooks.

This subpackage owns the engine-internal extension hooks: ``HookManager`` and
``HookType``.  They intercept engine control points — prompt assembly, memory
lifecycle ticks, and after-turn persistence — as opposed to the tool-lifecycle
hooks in :mod:`engine.execution.hooks.tool`, which intercept tool execution.

Historically these lived in a single flat ``hooks`` module alongside the tool
lifecycle system; the split is organizational only.  ``engine.execution.hooks``
re-exports everything for backward compatibility.
"""

from __future__ import annotations

from .manager import DEFAULT_HOOK_TIMEOUT_SECONDS, HookManager, HookType

__all__ = ["DEFAULT_HOOK_TIMEOUT_SECONDS", "HookManager", "HookType"]
