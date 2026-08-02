"""配置文件保护 Hook

阻止修改 linter、formatter、type checker 配置文件。
防止 Agent 为了通过检查而降低质量标准。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# 动态导入 PreToolHook（避免循环导入）
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
from engine.execution.tool_hooks import PreToolHook


class ConfigProtectionHook(PreToolHook):
    """配置文件保护 Hook

    阻止修改以下类型的配置文件：
    - Linter 配置：.eslintrc*, .flake8, ruff.toml, .pylintrc, golangci.yml
    - Formatter 配置：.prettierrc*, .black, .gofmt
    - Type checker 配置：tsconfig.json, pyproject.toml, mypy.ini
    """

    # 被保护的配置文件模式
    PROTECTED_CONFIG_PATTERNS = [
        # JavaScript/TypeScript
        ".eslintrc",
        "eslint.config",
        ".prettierrc",
        "prettier.config",
        "tsconfig.json",
        # Python
        ".flake8",
        "ruff.toml",
        ".pylintrc",
        "pylint.rc",
        "pyproject.toml",  # 可能包含 black、ruff、mypy 配置
        "mypy.ini",
        ".mypy.ini",
        # Go
        ".golangci.yml",
        ".golangci.yaml",
        # Rust
        "clippy.toml",
        ".clippy.toml",
        # 其他
        ".editorconfig",
    ]

    @property
    def id(self) -> str:
        return "config-protection"

    @property
    def priority(self) -> int:
        return 1  # 高优先级

    async def check(
        self,
        tool_name: str,
        tool_input: dict[str, Any]
    ) -> tuple[bool, str | None]:
        """检查是否尝试修改配置文件"""
        # 只检查文件编辑工具
        if tool_name not in ["Edit", "Write", "MultiEdit"]:
            return True, None

        file_path = tool_input.get("file_path", "")
        if not file_path:
            return True, None

        # 检查文件路径是否匹配被保护的配置文件
        file_path_lower = file_path.lower()
        for pattern in self.PROTECTED_CONFIG_PATTERNS:
            if pattern.lower() in file_path_lower:
                return False, (
                    f"Config file modification blocked: {file_path}\n\n"
                    f"This file appears to be a linter/formatter/type-checker config.\n"
                    f"Instead of modifying the config to pass checks, please fix the code.\n\n"
                    f"If you genuinely need to update the config, ask the user first."
                )

        return True, None
