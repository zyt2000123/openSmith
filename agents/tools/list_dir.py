"""List directory tool — tree-style directory listing."""
# 以受限深度和条目数展示目录结构，适合先建立项目全貌。

import asyncio
import os
from pathlib import Path

TOOL_META = {
    "name": "list_dir",
    "description": (
        "List files and directories in a path. Returns a tree-style view. "
        "Use to understand project structure before reading specific files."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory to list", "default": "."},
            "max_depth": {"type": "integer", "description": "Max directory depth (1-5)", "default": 2},
        },
        "required": [],
    },
    "path_args": ["path"],
    "permission_level": "read",
    "approval_policy": "never",
    "side_effect": "none",
    "execution_environment": "host",
}

MAX_ENTRIES = 300
EXCLUDED = {".git", "node_modules", "__pycache__", ".venv", "dist", ".build", ".DS_Store", ".egg-info"}


def _execute_sync(*, path: str = ".", max_depth: int = 2) -> str:
    if not isinstance(path, str):
        return "Error: path must be a string"
    if isinstance(max_depth, bool) or not isinstance(max_depth, int):
        return "Error: max_depth must be an integer"
    base = os.path.realpath(path)
    if not os.path.isdir(base):
        return f"Error: not a directory: {base}"

    base_path = Path(base).resolve()

    def is_safe_entry(entry_path: str) -> bool:
        """Only inspect non-symlink entries that resolve below the requested base."""
        if os.path.islink(entry_path):
            return False
        try:
            return Path(entry_path).resolve().is_relative_to(base_path)
        except (OSError, ValueError):
            return False

    max_depth = max(1, min(max_depth, 5))
    lines: list[str] = [f"# {base}"]
    count = 0

    def walk(dir_path: str, prefix: str, depth: int) -> None:
        nonlocal count
        if depth > max_depth or count >= MAX_ENTRIES:
            return
        try:
            entries = sorted(os.listdir(dir_path))
        except PermissionError:
            # Returning silently rendered an unreadable directory as an empty
            # one, so its contents vanished from the tree without a trace.
            lines.append(f"{prefix}[unreadable: permission denied]")
            return
        except OSError as e:
            lines.append(f"{prefix}[unreadable: {type(e).__name__}]")
            return

        dirs = [
            entry for entry in entries
            if entry not in EXCLUDED
            and is_safe_entry(os.path.join(dir_path, entry))
            and os.path.isdir(os.path.join(dir_path, entry))
        ]
        files = [
            entry for entry in entries
            if entry not in EXCLUDED
            and is_safe_entry(os.path.join(dir_path, entry))
            and os.path.isfile(os.path.join(dir_path, entry))
        ]

        for f in files:
            if count >= MAX_ENTRIES:
                return
            try:
                size = os.path.getsize(os.path.join(dir_path, f))
            except OSError:
                # The entry disappeared between listdir and stat. Skipping one
                # transient file beats aborting the whole listing over it.
                continue
            size_str = f"{size}B" if size < 1024 else f"{size/1024:.1f}K" if size < 1048576 else f"{size/1048576:.1f}M"
            lines.append(f"{prefix}{f}  {size_str}")
            count += 1

        for d in dirs:
            if count >= MAX_ENTRIES:
                return
            lines.append(f"{prefix}{d}/")
            count += 1
            walk(os.path.join(dir_path, d), prefix + "  ", depth + 1)

    walk(base, "", 1)
    if count >= MAX_ENTRIES:
        lines.append(f"\n...truncated at {MAX_ENTRIES} entries")
    return "\n".join(lines)


async def execute(*, path: str = ".", max_depth: int = 2) -> str:
    return await asyncio.to_thread(
        _execute_sync,
        path=path,
        max_depth=max_depth,
    )
