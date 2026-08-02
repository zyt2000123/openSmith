"""事实强制门禁 Hook

要求 Agent 在首次编辑文件前先进行充分调查。
防止在不理解代码的情况下盲目修改。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
from engine.execution.hooks import PreToolHook


class FactGateHook(PreToolHook):
    """事实强制门禁 Hook

    对每个文件，首次 Edit/Write 前要求：
    1. 已读取过该文件（Read 工具）
    2. 已搜索过相关内容（Grep、Find 等）

    这个 Hook 需要维护会话级别的状态，记录哪些文件已被调查。
    在真实实现中，应该从 session_context 中读取和更新状态。

    当前实现为简化版，假设在同一次工具调用序列中工作。
    """

    def __init__(self):
        super().__init__()
        # 会话级别的状态：已读取的文件集合
        # 实际应该从持久化的 session context 中读取
        self._investigated_files: set[str] = set()

    @property
    def id(self) -> str:
        return "fact-gate"

    @property
    def priority(self) -> int:
        return 2  # 在 config-protection 之后

    @property
    def enabled(self) -> bool:
        # 默认禁用，因为需要 session context 集成
        # 用户可以在配置中启用
        return False

    async def check(
        self,
        tool_name: str,
        tool_input: dict[str, Any]
    ) -> tuple[bool, str | None]:
        """检查是否在充分调查后才编辑"""
        # 只检查首次编辑
        if tool_name not in ["Edit", "Write"]:
            return True, None

        file_path = tool_input.get("file_path", "")
        if not file_path:
            return True, None

        # 检查是否已调查过该文件
        if file_path in self._investigated_files:
            return True, None

        # 首次编辑该文件，要求先调查
        return False, (
            f"First edit attempt blocked: {file_path}\n\n"
            f"Before editing a file for the first time, you must investigate:\n"
            f"1. Read the file to understand its current implementation\n"
            f"2. Search for importers to understand how it's used\n"
            f"3. Check related files and data schemas\n\n"
            f"Use Read, Grep, or Find tools to investigate, then try editing again."
        )

    def mark_file_investigated(self, file_path: str) -> None:
        """标记文件已被调查（应该在 Read 工具执行后调用）"""
        self._investigated_files.add(file_path)
