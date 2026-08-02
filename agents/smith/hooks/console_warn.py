"""Console.log 警告 Hook

检测并警告代码中的 console.log、print() 等调试语句。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
from engine.execution.hooks import PostToolHook


class ConsoleWarnHook(PostToolHook):
    """Console.log 警告 Hook

    检测常见的调试语句：
    - JavaScript/TypeScript: console.log, console.debug, console.info
    - Python: print()
    - Go: fmt.Println, fmt.Printf
    - Rust: println!, dbg!
    """

    # 调试语句模式（语言 -> 正则模式）
    DEBUG_PATTERNS = {
        "javascript": [
            r"console\.(log|debug|info|warn)\s*\(",
        ],
        "typescript": [
            r"console\.(log|debug|info|warn)\s*\(",
        ],
        "python": [
            r"\bprint\s*\(",
        ],
        "go": [
            r"fmt\.(Println|Printf|Print)\s*\(",
        ],
        "rust": [
            r"(println!|dbg!)\s*\(",
        ],
    }

    # 文件扩展名 -> 语言映射
    EXT_TO_LANG = {
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".py": "python",
        ".go": "go",
        ".rs": "rust",
    }

    @property
    def id(self) -> str:
        return "console-warn"

    async def check(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_output: Any
    ) -> list[str]:
        """检查编辑的代码是否包含调试语句"""
        # 只检查 edit_file 工具（Agent-Smith 工具名；路径参数键是 path）
        if tool_name != "edit_file":
            return []

        file_path = tool_input.get("path", "")
        new_string = tool_input.get("new_string", "")

        if not file_path or not new_string:
            return []

        # 根据文件扩展名确定语言
        file_ext = Path(file_path).suffix.lower()
        language = self.EXT_TO_LANG.get(file_ext)

        if not language:
            return []

        # 获取该语言的调试语句模式
        patterns = self.DEBUG_PATTERNS.get(language, [])
        if not patterns:
            return []

        # 检测调试语句
        found_statements = []
        for pattern in patterns:
            matches = re.findall(pattern, new_string, re.IGNORECASE)
            if matches:
                # 提取匹配到的语句（去重）
                for match in matches:
                    if isinstance(match, tuple):
                        stmt = match[0] if match else pattern
                    else:
                        stmt = match
                    if stmt not in found_statements:
                        found_statements.append(stmt)

        if not found_statements:
            return []

        # 生成警告信息
        statements_str = ", ".join(found_statements)
        warning = (
            f"⚠️  Debug statement detected in {file_path}: {statements_str}\n\n"
            f"Debug statements should not be committed to the codebase.\n"
            f"Please use proper logging instead:\n"
        )

        if language in ["javascript", "typescript"]:
            warning += "  - Use a logging library (e.g., winston, pino)\n"
            warning += "  - Or remove the debug statement if no longer needed\n"
        elif language == "python":
            warning += "  - Use logging module: logger.info(), logger.debug()\n"
            warning += "  - Or remove the print() if no longer needed\n"
        elif language == "go":
            warning += "  - Use log package: log.Printf(), log.Println()\n"
            warning += "  - Or remove the fmt.Println if no longer needed\n"
        elif language == "rust":
            warning += "  - Use log crate: info!(), debug!()\n"
            warning += "  - Or remove the println!/dbg! if no longer needed\n"

        return [warning]
