"""Hook 注册中心和执行管理器

HookRegistry 负责：
1. 注册 Hook 实例
2. 按优先级执行 Pre Hook
3. 执行 Post Hook（支持异步）
4. 执行 Stop Hook（支持异步）
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .hook_interface import PostToolHook, PreToolHook, StopHook

logger = logging.getLogger(__name__)


class HookRegistry:
    """Hook 注册中心

    管理所有已注册的 Hook，提供统一的执行接口。
    """

    def __init__(self) -> None:
        self._pre_hooks: dict[str, PreToolHook] = {}
        self._post_hooks: dict[str, PostToolHook] = {}
        self._stop_hooks: dict[str, StopHook] = {}

    def register_pre_hook(self, hook: PreToolHook) -> None:
        """注册 Pre Hook"""
        if hook.id in self._pre_hooks:
            logger.warning("Pre hook %s already registered, replacing", hook.id)
        self._pre_hooks[hook.id] = hook
        logger.debug("Registered pre hook: %s (priority=%d)", hook.id, hook.priority)

    def register_post_hook(self, hook: PostToolHook) -> None:
        """注册 Post Hook"""
        if hook.id in self._post_hooks:
            logger.warning("Post hook %s already registered, replacing", hook.id)
        self._post_hooks[hook.id] = hook
        logger.debug("Registered post hook: %s", hook.id)

    def register_stop_hook(self, hook: StopHook) -> None:
        """注册 Stop Hook"""
        if hook.id in self._stop_hooks:
            logger.warning("Stop hook %s already registered, replacing", hook.id)
        self._stop_hooks[hook.id] = hook
        logger.debug("Registered stop hook: %s", hook.id)

    def get_pre_hook(self, hook_id: str) -> PreToolHook | None:
        """获取指定的 Pre Hook"""
        return self._pre_hooks.get(hook_id)

    def get_post_hook(self, hook_id: str) -> PostToolHook | None:
        """获取指定的 Post Hook"""
        return self._post_hooks.get(hook_id)

    def get_stop_hook(self, hook_id: str) -> StopHook | None:
        """获取指定的 Stop Hook"""
        return self._stop_hooks.get(hook_id)

    async def run_pre_hooks(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        enabled_hook_ids: list[str] | None = None
    ) -> tuple[bool, str | None]:
        """执行所有已启用的 Pre Hook

        按 priority 从小到大顺序执行。一旦某个 Hook 返回 False，立即停止执行。

        Args:
            tool_name: 工具名称
            tool_input: 工具输入参数
            enabled_hook_ids: 启用的 Hook ID 列表，None 表示所有已注册的 Hook

        Returns:
            (allowed, denial_reason)
            - allowed=True: 所有 Hook 都通过
            - allowed=False: 某个 Hook 拒绝，denial_reason 为拒绝原因
        """
        if enabled_hook_ids is None:
            hooks = [h for h in self._pre_hooks.values() if h.enabled]
        else:
            hooks = [
                self._pre_hooks[hid]
                for hid in enabled_hook_ids
                if hid in self._pre_hooks and self._pre_hooks[hid].enabled
            ]

        # 按优先级排序（数字越小越先执行）
        sorted_hooks = sorted(hooks, key=lambda h: h.priority)

        for hook in sorted_hooks:
            try:
                allowed, reason = await hook.check(tool_name, tool_input)
                if not allowed:
                    logger.info(
                        "Pre hook %s blocked tool %s: %s",
                        hook.id,
                        tool_name,
                        reason
                    )
                    return False, f"[{hook.id}] {reason}"
            except Exception as e:
                logger.error(
                    "Pre hook %s failed: %s",
                    hook.id,
                    str(e),
                    exc_info=True
                )
                # Pre Hook 执行失败不应阻止工具调用，记录错误后继续
                continue

        return True, None

    async def run_post_hooks(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_output: Any,
        enabled_hook_ids: list[str] | None = None
    ) -> list[str]:
        """执行所有已启用的 Post Hook

        Args:
            tool_name: 工具名称
            tool_input: 工具输入参数
            tool_output: 工具执行结果
            enabled_hook_ids: 启用的 Hook ID 列表，None 表示所有已注册的 Hook

        Returns:
            warnings: 所有 Hook 返回的警告信息合集
        """
        if enabled_hook_ids is None:
            hooks = [h for h in self._post_hooks.values() if h.enabled]
        else:
            hooks = [
                self._post_hooks[hid]
                for hid in enabled_hook_ids
                if hid in self._post_hooks and self._post_hooks[hid].enabled
            ]

        all_warnings: list[str] = []
        async_tasks: list[asyncio.Task] = []

        for hook in hooks:
            try:
                if hook.async_execution:
                    # 异步执行：不阻塞本轮工具调用返回，但必须保留任务引用并
                    # 在返回前等待，否则任务可能被 GC 中途销毁（"Task was
                    # destroyed but it is pending"），或等待中的子进程被孤儿化。
                    task = asyncio.create_task(
                        self._run_post_hook_async(
                            hook, tool_name, tool_input, tool_output
                        )
                    )
                    async_tasks.append(task)
                else:
                    # 同步执行
                    warnings = await hook.check(tool_name, tool_input, tool_output)
                    all_warnings.extend(warnings)
            except Exception as e:
                logger.error(
                    "Post hook %s failed: %s",
                    hook.id,
                    str(e),
                    exc_info=True
                )
                # Post Hook 失败不应中断流程，记录错误后继续

        # 等待异步任务完成并收集其警告（契约：警告须注入 Agent 下一轮输入）。
        for task in async_tasks:
            try:
                warnings = await task
                if warnings:
                    all_warnings.extend(warnings)
            except asyncio.CancelledError:
                task.cancel()
                raise
            except Exception as e:
                logger.error("Async post hook failed: %s", e, exc_info=True)
        return all_warnings

    async def _run_post_hook_async(
        self,
        hook: PostToolHook,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_output: Any
    ) -> list[str]:
        """异步执行 Post Hook（在后台）"""
        try:
            warnings = await hook.check(tool_name, tool_input, tool_output)
            if warnings:
                logger.info(
                    "Post hook %s (async) generated warnings: %s",
                    hook.id,
                    warnings
                )
            return warnings
        except Exception as e:
            logger.error(
                "Async post hook %s failed: %s",
                hook.id,
                str(e),
                exc_info=True
            )
            return []

    async def run_stop_hooks(
        self,
        session_id: str,
        session_context: dict[str, Any],
        enabled_hook_ids: list[str] | None = None
    ) -> None:
        """执行所有已启用的 Stop Hook

        Args:
            session_id: 会话 ID
            session_context: 会话上下文
            enabled_hook_ids: 启用的 Hook ID 列表，None 表示所有已注册的 Hook
        """
        if enabled_hook_ids is None:
            hooks = [h for h in self._stop_hooks.values() if h.enabled]
        else:
            hooks = [
                self._stop_hooks[hid]
                for hid in enabled_hook_ids
                if hid in self._stop_hooks and self._stop_hooks[hid].enabled
            ]

        async_tasks: list[asyncio.Task] = []

        for hook in hooks:
            try:
                if hook.async_execution:
                    # 异步执行：保留任务引用并在返回前等待，避免任务被 GC 中途
                    # 销毁或等待中的子进程被孤儿化。
                    task = asyncio.create_task(
                        self._run_stop_hook_async(
                            hook, session_id, session_context
                        )
                    )
                    async_tasks.append(task)
                else:
                    # 同步执行
                    await hook.run(session_id, session_context)
                    logger.debug("Stop hook %s completed", hook.id)
            except Exception as e:
                logger.error(
                    "Stop hook %s failed: %s",
                    hook.id,
                    str(e),
                    exc_info=True
                )

        for task in async_tasks:
            try:
                await task
            except asyncio.CancelledError:
                task.cancel()
                raise
            except Exception as e:
                logger.error("Async stop hook failed: %s", e, exc_info=True)

    async def _run_stop_hook_async(
        self,
        hook: StopHook,
        session_id: str,
        session_context: dict[str, Any]
    ) -> None:
        """异步执行 Stop Hook（在后台）"""
        try:
            await hook.run(session_id, session_context)
            logger.debug("Async stop hook %s completed", hook.id)
        except Exception as e:
            logger.error(
                "Async stop hook %s failed: %s",
                hook.id,
                str(e),
                exc_info=True
            )

    def list_registered_hooks(self) -> dict[str, list[str]]:
        """列出所有已注册的 Hook"""
        return {
            "pre": list(self._pre_hooks.keys()),
            "post": list(self._post_hooks.keys()),
            "stop": list(self._stop_hooks.keys()),
        }
