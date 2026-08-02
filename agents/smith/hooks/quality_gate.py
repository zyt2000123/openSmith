"""质量门检查 Hook

对编辑的文件运行基础质量检查（格式化、lint）。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
from engine.execution.hooks import PostToolHook

# 单次格式化/lint 检查的硬超时：npx 首次运行可能下载依赖，但没有上限会挂死
# 事件循环。 超时后放弃本次检查，不阻塞 Agent 响应。
_CHECK_TIMEOUT_SECONDS = 15.0

# 允许转发给格式化/lint 子进程的父环境键。 其余一律不放行，避免把引擎持有的
# API key / 数据库凭据泄漏给 npx/black/ruff 等任意工具进程。
_SAFE_ENV_KEYS = ("LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "NO_COLOR")


def _minimal_environment() -> dict[str, str]:
    """构造格式化/lint 子进程的最小环境（PATH + 少量安全键）。"""
    environment: dict[str, str] = {
        "PATH": os.environ.get("PATH") or os.defpath,
        "HOME": os.environ.get("HOME") or "/",
    }
    for key in _SAFE_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            environment[key] = value
    return environment


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
        # 只检查文件编辑工具（Agent-Smith 工具名；路径参数键是 path）
        if tool_name not in ["edit_file", "write_file"]:
            return []

        file_path = tool_input.get("path", "")
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
        try:
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

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(Path(file_path).resolve().parent),
                env=_minimal_environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=_CHECK_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return f"⚠️  Format check timed out for {file_path}"

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

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(Path(file_path).resolve().parent),
                env=_minimal_environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=_CHECK_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return f"⚠️  Lint check timed out for {file_path}"

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
