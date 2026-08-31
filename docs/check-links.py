#!/usr/bin/env python3
"""检查 docs/ 下所有 Markdown 相对链接是否可解析。

维护规则第 5 条要求链接保持可解析，但仓库没有 CI，所以这条规则此前没有执行者。
改名或移动文档后跑一次：

    python3 docs/check-links.py

退出码 0 表示全部可解析，1 表示有断链（断链逐条打印）。
只用标准库，只读文件系统。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent

# 跳过外链、页内锚点和 mailto；只校验指向文件的相对路径。
LINK = re.compile(r"\[[^\]]*\]\((?!https?:|#|mailto:)([^)#\s]+)(?:#[^)]*)?\)")
# 围栏代码块里的方括号常常是正则或示例，不是链接。
FENCE = re.compile(r"^\s*(```|~~~)")


def links_outside_code(text: str):
    """逐行产出 (行号, 链接目标)，跳过围栏代码块内的内容。"""
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in LINK.finditer(line):
            yield lineno, match.group(1).strip()


def main() -> int:
    broken: list[str] = []
    checked = 0
    for md in sorted(DOCS.rglob("*.md")):
        for lineno, target in links_outside_code(md.read_text(encoding="utf-8")):
            checked += 1
            if not (md.parent / target).resolve().exists():
                broken.append(f"{md.relative_to(DOCS)}:{lineno} → {target}")

    for item in broken:
        print(f"✗ {item}")
    print(f"\n{checked} 条相对链接，断链 {len(broken)} 条")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
