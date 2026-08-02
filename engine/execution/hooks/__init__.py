"""Hook 系统模块

提供完整的 Hook 基础设施，单一导入路径 ``engine.execution.hooks``：

- 工具生命周期（tool lifecycle）: ``PreToolHook`` / ``PostToolHook`` /
  ``StopHook`` + ``HookRegistry`` + ``HookLoader`` —— 在工具执行前/后/会话
  结束时拦截。
- 引擎内部扩展（engine extension）: ``HookManager`` / ``HookType`` —— 改写
  prompt、memory 生命周期 tick、after-turn 持久化。

两套接口职责不同但共享同一包，统一由 `engine.execution.hooks` 一个入口导出。
"""

from .hook_interface import PostToolHook, PreToolHook, StopHook
from .hook_loader import HookLoader
from .hook_manager import HookRegistry
from .engine_hooks import (
    DEFAULT_HOOK_TIMEOUT_SECONDS,
    HookManager,
    HookType,
)

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
