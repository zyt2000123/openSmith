"""质量门检查 Hook

对编辑的文件运行基础质量检查（格式化、lint）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
from engine.execution.hooks import PostToolHook


class QualityGateHook(PostToolHook):
    """质量门检查 Hook

    对编辑后的文件运行：
    - 格式化检查（prettier、black、gofmt 等）
    - Lint 检查（eslint、ruff、golangci-lint 等）

    注意：此 Hook 异步执行，不阻塞 Agent 响应。
    实际的格式化和 lint 命令执行需要项目中存在相应工具。
    """

    # 文件扩展名 -> 格式化工具映射
    FORMATTERS = {
        ".js": "prettier",
        ".jsx": "prettier",
        ".ts": "prettier",
        ".tsx": "prettier",
        ".py": "black",
        ".go": "gofmt",
        ".rs": "rustfmt",
    }

    # 文件扩展名 -> Linter 映射
    LINTERS = {
        ".js": "eslint",
        ".jsx": "eslint",
        ".ts": "eslint",
        ".tsx": "eslint",
        ".py": "ruff",
        ".go": "golangci-lint",
        ".rs": "clippy",
    }

    @property
    def id(self) -> str:
        return "quality-gate"

    @property
    def async_execution(self) -> bool:
        return True  # 异步执行，不阻塞响应

    async def check(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_output: Any
    ) -> list[str]:
        """运行质量检查"""
        # 只检查文件编辑工具
        if tool_name not in ["Edit", "Write", "MultiEdit"]:
            return []

        file_path = tool_input.get("file_path", "")
        if not file_path:
            return []

        file_ext = Path(file_path).suffix.lower()
        warnings = []

        # 运行格式化检查
        formatter = self.FORMATTERS.get(file_ext)
        if formatter:
            format_warning = await self._check_format(file_path, formatter)
            if format_warning:
                warnings.append(format_warning)

        # 运行 Lint 检查
        linter = self.LINTERS.get(file_ext)
        if linter:
            lint_warning = await self._check_lint(file_path, linter)
            if lint_warning:
                warnings.append(lint_warning)

        return warnings

    async def _check_format(self, file_path: str, formatter: str) -> str | None:
        """检查文件格式（简化实现）"""
        # 实际实现应该调用相应的格式化工具
        # 这里返回一个提示信息
        try:
            # 尝试运行格式化工具检查（--check 模式）
            if formatter == "prettier":
                cmd = ["npx", "prettier", "--check", file_path]
            elif formatter == "black":
                cmd = ["black", "--check", file_path]
            elif formatter == "gofmt":
                cmd = ["gofmt", "-l", file_path]
            elif formatter == "rustfmt":
                cmd = ["rustfmt", "--check", file_path]
            else:
                return None

            # 使用 asyncio.create_subprocess_exec（安全的参数化执行）
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                return (
                    f"⚠️  Format check failed for {file_path}\n"
                    f"Run formatter to auto-fix."
                )

            return None

        except FileNotFoundError:
            # 格式化工具未安装，跳过检查
            return None
        except Exception:
            # 检查失败不应阻塞流程
            return None

    async def _check_lint(self, file_path: str, linter: str) -> str | None:
        """检查 Lint 错误（简化实现）"""
        try:
            if linter == "eslint":
                cmd = ["npx", "eslint", file_path]
            elif linter == "ruff":
                cmd = ["ruff", "check", file_path]
            elif linter == "golangci-lint":
                cmd = ["golangci-lint", "run", file_path]
            elif linter == "clippy":
                cmd = ["cargo", "clippy"]
            else:
                return None

            # 使用 asyncio.create_subprocess_exec（安全的参数化执行）
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                output = stdout.decode() if stdout else stderr.decode()
                # 截取前 500 字符避免过长
                output_preview = output[:500] + "..." if len(output) > 500 else output
                return (
                    f"⚠️  Lint check failed for {file_path}\n"
                    f"{output_preview}"
                )

            return None

        except FileNotFoundError:
            # Linter 未安装，跳过检查
            return None
        except Exception:
            # 检查失败不应阻塞流程
            return None
