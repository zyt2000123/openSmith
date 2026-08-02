"""Hook 动态加载器

从配置文件加载并实例化 Hook 类。
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

import yaml

from .hook_interface import PostToolHook, PreToolHook, StopHook
from .hook_manager import HookRegistry

logger = logging.getLogger(__name__)


class HookLoader:
    """Hook 动态加载器

    从 YAML 配置文件加载 Hook 定义，动态导入并实例化 Hook 类。
    """

    def load_hooks_from_config(
        self,
        config_path: Path,
        registry: HookRegistry
    ) -> None:
        """从配置文件加载并注册 Hook

        Args:
            config_path: hooks.yaml 配置文件路径
            registry: Hook 注册中心

        配置文件格式:
            hooks:
              pre:
                - id: config-protection
                  enabled: true
                  module: "agents/smith/hooks/config_protection.py"
                  class: "ConfigProtectionHook"
                  priority: 1
              post:
                - id: console-warn
                  enabled: true
                  module: "agents/smith/hooks/console_warn.py"
                  class: "ConsoleWarnHook"
              stop:
                - id: cost-tracker
                  enabled: true
                  module: "agents/smith/hooks/cost_tracker.py"
                  class: "CostTrackerHook"
        """
        if not config_path.exists():
            logger.warning("Hook config file not found: %s", config_path)
            return

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
        except Exception as e:
            logger.error("Failed to load hook config: %s", e, exc_info=True)
            return

        if not config or "hooks" not in config:
            logger.warning("No hooks defined in config: %s", config_path)
            return

        hooks_config = config["hooks"]

        # 加载 Pre Hook
        if "pre" in hooks_config:
            for hook_def in hooks_config["pre"]:
                if not hook_def.get("enabled", True):
                    logger.debug("Skipping disabled pre hook: %s", hook_def.get("id"))
                    continue

                try:
                    hook = self._load_pre_hook(hook_def, config_path.parent)
                    if hook:
                        registry.register_pre_hook(hook)
                except Exception as e:
                    logger.error(
                        "Failed to load pre hook %s: %s",
                        hook_def.get("id"),
                        e,
                        exc_info=True
                    )

        # 加载 Post Hook
        if "post" in hooks_config:
            for hook_def in hooks_config["post"]:
                if not hook_def.get("enabled", True):
                    logger.debug("Skipping disabled post hook: %s", hook_def.get("id"))
                    continue

                try:
                    hook = self._load_post_hook(hook_def, config_path.parent)
                    if hook:
                        registry.register_post_hook(hook)
                except Exception as e:
                    logger.error(
                        "Failed to load post hook %s: %s",
                        hook_def.get("id"),
                        e,
                        exc_info=True
                    )

        # 加载 Stop Hook
        if "stop" in hooks_config:
            for hook_def in hooks_config["stop"]:
                if not hook_def.get("enabled", True):
                    logger.debug("Skipping disabled stop hook: %s", hook_def.get("id"))
                    continue

                try:
                    hook = self._load_stop_hook(hook_def, config_path.parent)
                    if hook:
                        registry.register_stop_hook(hook)
                except Exception as e:
                    logger.error(
                        "Failed to load stop hook %s: %s",
                        hook_def.get("id"),
                        e,
                        exc_info=True
                    )

        logger.info(
            "Loaded hooks from %s: %s",
            config_path,
            registry.list_registered_hooks()
        )

    def _load_pre_hook(
        self,
        hook_def: dict[str, Any],
        config_dir: Path
    ) -> PreToolHook | None:
        """加载单个 Pre Hook"""
        module_path = hook_def.get("module")
        class_name = hook_def.get("class")

        if not module_path or not class_name:
            logger.error(
                "Pre hook %s missing module or class",
                hook_def.get("id")
            )
            return None

        hook_class = self._load_hook_class(module_path, class_name, config_dir)
        if not hook_class:
            return None

        if not issubclass(hook_class, PreToolHook):
            logger.error(
                "Class %s is not a PreToolHook subclass",
                class_name
            )
            return None

        # 实例化 Hook
        hook_instance = hook_class()
        return hook_instance

    def _load_post_hook(
        self,
        hook_def: dict[str, Any],
        config_dir: Path
    ) -> PostToolHook | None:
        """加载单个 Post Hook"""
        module_path = hook_def.get("module")
        class_name = hook_def.get("class")

        if not module_path or not class_name:
            logger.error(
                "Post hook %s missing module or class",
                hook_def.get("id")
            )
            return None

        hook_class = self._load_hook_class(module_path, class_name, config_dir)
        if not hook_class:
            return None

        if not issubclass(hook_class, PostToolHook):
            logger.error(
                "Class %s is not a PostToolHook subclass",
                class_name
            )
            return None

        hook_instance = hook_class()
        return hook_instance

    def _load_stop_hook(
        self,
        hook_def: dict[str, Any],
        config_dir: Path
    ) -> StopHook | None:
        """加载单个 Stop Hook"""
        module_path = hook_def.get("module")
        class_name = hook_def.get("class")

        if not module_path or not class_name:
            logger.error(
                "Stop hook %s missing module or class",
                hook_def.get("id")
            )
            return None

        hook_class = self._load_hook_class(module_path, class_name, config_dir)
        if not hook_class:
            return None

        if not issubclass(hook_class, StopHook):
            logger.error(
                "Class %s is not a StopHook subclass",
                class_name
            )
            return None

        hook_instance = hook_class()
        return hook_instance

    def _load_hook_class(
        self,
        module_path: str,
        class_name: str,
        config_dir: Path
    ) -> type | None:
        """动态导入 Hook 类

        Args:
            module_path: 模块路径（相对于项目根目录或绝对路径）
            class_name: 类名
            config_dir: 配置文件所在目录（用于解析相对路径）

        Returns:
            Hook 类，如果加载失败则返回 None
        """
        # 解析模块文件路径
        module_file = Path(module_path)

        # 如果是相对路径，尝试相对于配置文件目录解析
        if not module_file.is_absolute():
            # 先尝试相对于项目根目录
            project_root = Path(__file__).parent.parent.parent.parent
            resolved = project_root / module_path
            if not resolved.exists():
                # 再尝试相对于配置文件目录
                resolved = config_dir / module_path
            module_file = resolved

        if not module_file.exists():
            logger.error("Module file not found: %s", module_file)
            return None

        try:
            # 动态导入模块
            spec = importlib.util.spec_from_file_location(
                f"hook_module_{class_name}",
                module_file
            )
            if not spec or not spec.loader:
                logger.error("Failed to load spec for %s", module_file)
                return None

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # 获取类
            if not hasattr(module, class_name):
                logger.error(
                    "Class %s not found in module %s",
                    class_name,
                    module_file
                )
                return None

            hook_class = getattr(module, class_name)
            return hook_class

        except Exception as e:
            logger.error(
                "Failed to load class %s from %s: %s",
                class_name,
                module_file,
                e,
                exc_info=True
            )
            return None
