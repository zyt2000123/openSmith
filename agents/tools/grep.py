"""Grep tool — search file contents using ripgrep (rg) or fallback to grep."""

import asyncio
import os
import subprocess

TOOL_META = {
    "name": "grep",
    "description": (
        "Search file contents for a pattern. Uses ripgrep (rg) if available, "
        "falls back to grep. Returns matching file paths with line numbers and context. "
        "Automatically skips .git, node_modules, __pycache__, .venv, and binary files."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Search pattern (regex supported)"},
            "path": {"type": "string", "description": "Directory or file to search in", "default": "."},
            "include": {"type": "string", "description": "File glob filter, e.g. '*.py'"},
            "ignore_case": {"type": "boolean", "default": False},
            "context_lines": {"type": "integer", "description": "Lines of context (0-5)", "default": 0},
            "files_only": {"type": "boolean", "description": "Only list matching file paths", "default": False},
        },
        "required": ["pattern"],
    },
    "path_args": ["path"],
    "permission_level": "read",
    "approval_policy": "never",
    "side_effect": "none",
    "execution_environment": "host",
}

MAX_RESULTS = 200
MAX_LINE_LEN = 500
EXCLUDED = [".git", "node_modules", "__pycache__", ".venv", "dist", ".build", "*.egg-info"]


def _has_rg() -> bool:
    try:
        subprocess.run(["rg", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _execute_sync(
    *, pattern: str, path: str = ".", include: str = "",
    ignore_case: bool = False, context_lines: int = 0, files_only: bool = False,
) -> str:
    include = include or None
    if not pattern.strip():
        return "Error: pattern is required"
    resolved = os.path.abspath(path)
    if not os.path.exists(resolved):
        return f"Error: path not found: {resolved}"

    use_rg = _has_rg()
    if use_rg:
        args = ["rg", "--hidden", "--max-columns", str(MAX_LINE_LEN), "--max-count", "50"]
        for e in EXCLUDED:
            args.extend(["--glob", f"!{e}"])
        if ignore_case: args.append("-i")
        if files_only: args.append("-l")
        else:
            args.append("-n")
            if context_lines > 0: args.extend(["-C", str(min(context_lines, 5))])
        if include: args.extend(["--glob", include])
        args.extend(["-e", pattern] if pattern.startswith("-") else [pattern])
        args.append(resolved)
    else:
        args = ["grep", "-r", "--binary-files=without-match"]
        for e in EXCLUDED:
            args.extend(["--exclude-dir", e])
        if ignore_case: args.append("-i")
        if files_only: args.append("-l")
        else:
            args.append("-n")
            if context_lines > 0: args.extend(["-C", str(min(context_lines, 5))])
        if include: args.extend(["--include", include])
        args.extend(["-e", pattern] if pattern.startswith("-") else [pattern])
        args.append(resolved)

    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return "Error: search timed out. Try a more specific pattern or path."

    output = result.stdout.strip()
    if not output:
        return f"No matches found for: {pattern}"

    lines = output.split("\n")
    total = len(lines)
    lines = [l[:MAX_LINE_LEN] + "…" if len(l) > MAX_LINE_LEN else l for l in lines[:MAX_RESULTS]]
    header = f"# grep {'(rg)' if use_rg else ''}: {min(total, MAX_RESULTS)} results"
    if total > MAX_RESULTS:
        header += f" (showing {MAX_RESULTS} of {total})"
    return header + "\n" + "\n".join(lines)


async def execute(
    *, pattern: str, path: str = ".", include: str = "",
    ignore_case: bool = False, context_lines: int = 0, files_only: bool = False,
) -> str:
    return await asyncio.to_thread(
        _execute_sync,
        pattern=pattern,
        path=path,
        include=include,
        ignore_case=ignore_case,
        context_lines=context_lines,
        files_only=files_only,
    )
