"""Abstract interfaces for the three tool-lifecycle hook types.

- ``PreToolHook``: intercept before tool execution (may block the call).
- ``PostToolHook``: observe after tool execution (may only warn).
- ``StopHook``: batched processing when the session stops.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class PreToolHook(ABC):
    """工具执行前的拦截 Hook

    Pre Hook 可以阻止工具执行，用于安全防护和策略强制。
    """

    def __init__(self) -> None:
        # YAML 配置的优先级（数字越小越先执行）；加载器构建实例后注入。
        # 类级 ``priority`` property 仍是默认值，配置注入优先于它。
        self._configured_priority: int | None = None

    @property
    @abstractmethod
    def id(self) -> str:
        """Hook 唯一标识符"""
        pass

    @property
    def priority(self) -> int:
        """优先级（数字越小越先执行）

        默认为 100。关键安全检查应设置较小的值。YAML 配置的 priority 优先。
        """
        return self._configured_priority if self._configured_priority is not None else 100

    @property
    def enabled(self) -> bool:
        """Hook 是否启用（默认启用）"""
        return True

    @abstractmethod
    async def check(
        self,
        tool_name: str,
        tool_input: dict[str, Any]
    ) -> tuple[bool, str | None]:
        """检查工具调用是否允许执行

        Args:
            tool_name: 工具名称（如 "edit_file", "shell"）
            tool_input: 工具输入参数

        Returns:
            (allowed, denial_reason)
            - allowed=True: 允许执行
            - allowed=False: 阻止执行，denial_reason 为拒绝原因
        """
        pass


class PostToolHook(ABC):
    """工具执行后的观察 Hook

    Post Hook 不能阻止已执行的操作，只能发出警告或记录。
    """

    @property
    @abstractmethod
    def id(self) -> str:
        """Hook 唯一标识符"""
        pass

    @property
    def enabled(self) -> bool:
        """Hook 是否启用（默认启用）"""
        return True

    @property
    def async_execution(self) -> bool:
        """是否异步执行（不阻塞响应）

        默认为 False（同步执行）。
        设置为 True 时，Hook 在后台执行，不会阻塞 Agent 响应。
        """
        return False

    @abstractmethod
    async def check(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_output: Any
    ) -> list[str]:
        """检查工具执行结果并返回警告

        Args:
            tool_name: 工具名称
            tool_input: 工具输入参数
            tool_output: 工具执行结果

        Returns:
            warnings: 警告信息列表（注入到 Agent 下一轮输入）
        """
        pass


class StopHook(ABC):
    """会话响应结束时的批量处理 Hook

    Stop Hook 在每轮 Agent 响应结束时触发，用于：
    - 批量处理（如批量格式化）
    - 成本跟踪
    - 状态持久化
    """

    @property
    @abstractmethod
    def id(self) -> str:
        """Hook 唯一标识符"""
        pass

    @property
    def enabled(self) -> bool:
        """Hook 是否启用（默认启用）"""
        return True

    @property
    def async_execution(self) -> bool:
        """是否异步执行（不阻塞响应）

        默认为 True（异步执行）。
        Stop Hook 通常不需要阻塞用户，可在后台完成。
        """
        return True

    @abstractmethod
    async def run(
        self,
        session_id: str,
        session_context: dict[str, Any]
    ) -> None:
        """执行 Stop Hook

        Args:
            session_id: 会话 ID
            session_context: 会话上下文，包含：
                - edited_files: 本轮编辑的文件列表
                - tool_calls: 本轮工具调用列表
                - session_stats: 会话统计信息（token、成本等）
        """
        pass
