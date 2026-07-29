"""Read file tool provider — reads local file content with safety limits."""

import asyncio
import os

TOOL_META = {
    "name": "read_file",
    "description": "Read the content of a local file. Returns text content with line numbers.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute or relative path to the file to read"
            },
            "offset": {
                "type": "integer",
                "description": "Line number to start reading from (0-based)",
                "default": 0
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of lines to read",
                "default": 500
            }
        },
        "required": ["path"]
    },
    "path_args": ["path"],
    "permission_level": "read",
    "approval_policy": "never",
    "side_effect": "none",
    "execution_environment": "host",
}

MAX_READ_BYTES = 50 * 1024  # 50KB preview budget per call
MAX_LIMIT = 2000


def _execute_sync(*, path: str, offset: int = 0, limit: int = 500) -> str:
    resolved = os.path.realpath(path)

    if not os.path.exists(resolved):
        return f"Error: file not found: {resolved}"

    if not os.path.isfile(resolved):
        return f"Error: not a regular file: {resolved}"

    start = max(0, offset)
    limit = min(max(1, limit), MAX_LIMIT)

    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            selected: list[str] = []
            selected_bytes = 0
            last_line = start
            hit_byte_limit = False

            for line_no, line in enumerate(f, start=1):
                if line_no <= start:
                    continue
                if len(selected) >= limit:
                    break

                encoded = line.encode("utf-8")
                line_bytes = len(encoded)
                if selected_bytes + line_bytes > MAX_READ_BYTES:
                    if not selected:
                        remaining = max(1, MAX_READ_BYTES - selected_bytes)
                        line = encoded[:remaining].decode("utf-8", errors="replace")
                        selected.append(line)
                        last_line = line_no
                    hit_byte_limit = True
                    break

                selected.append(line)
                selected_bytes += line_bytes
                last_line = line_no

                if selected_bytes >= MAX_READ_BYTES:
                    hit_byte_limit = True
                    break
    except PermissionError:
        return f"Error: permission denied: {resolved}"
    except Exception as e:
        return f"Error reading file: {e}"

    numbered = []
    for i, line in enumerate(selected, start=start + 1):
        numbered.append(f"{i}\t{line.rstrip()}")

    if selected:
        header = f"# {resolved} (showing lines {start + 1}-{last_line})"
    else:
        header = f"# {resolved} (no lines from offset {start})"
    if hit_byte_limit:
        header += f" — stopped at {MAX_READ_BYTES} byte preview limit"
    elif len(selected) >= limit:
        header += f" — stopped at {limit} line limit"
    return header + "\n" + "\n".join(numbered)


async def execute(*, path: str, offset: int = 0, limit: int = 500) -> str:
    return await asyncio.to_thread(
        _execute_sync,
        path=path,
        offset=offset,
        limit=limit,
    )
