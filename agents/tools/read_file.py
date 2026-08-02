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
    if not isinstance(path, str):
        return "Error: path must be a string"
    if isinstance(offset, bool) or not isinstance(offset, int):
        return "Error: offset must be an integer"
    if isinstance(limit, bool) or not isinstance(limit, int):
        return "Error: limit must be an integer"
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
            line_no = 0

            while len(selected) < limit:
                # A bounded readline window keeps a single over-long line from
                # ever entering memory whole: the 50 KB preview budget used to
                # be applied only after the full line had already been read and
                # materialized.
                chunk = f.readline(MAX_READ_BYTES + 1)
                if not chunk:
                    break
                line_no += 1
                if line_no <= start:
                    # Skip the line; a giant skipped line is drained in bounded
                    # slices rather than materialized.
                    while not chunk.endswith(("\n", "\r")):
                        rest = f.readline(MAX_READ_BYTES)
                        if not rest:
                            break
                        chunk = rest
                    continue

                encoded = chunk.encode("utf-8")
                line_bytes = len(encoded)
                if line_bytes > MAX_READ_BYTES and not chunk.endswith(("\n", "\r")):
                    # One over-long line: emit a truncated preview and stop.
                    # The remainder is deliberately not read, so the true length
                    # is unknown — reporting a number would be a guess.
                    remaining = max(1, MAX_READ_BYTES - selected_bytes)
                    line = encoded[:remaining].decode("utf-8", errors="replace")
                    selected.append(
                        f"{line}…[line truncated at {remaining} bytes; line exceeds "
                        f"{MAX_READ_BYTES // 1024} KB]"
                    )
                    last_line = line_no
                    hit_byte_limit = True
                    break

                if selected_bytes + line_bytes > MAX_READ_BYTES:
                    if not selected:
                        remaining = max(1, MAX_READ_BYTES - selected_bytes)
                        line = encoded[:remaining].decode("utf-8", errors="replace")
                        # Mark the cut inline. The header's line range alone made
                        # a mid-token truncation of one very long line (minified
                        # JS, base64, single-line JSON) look like a line that
                        # simply ended there.
                        selected.append(
                            f"{line}…[line truncated at {remaining} of {line_bytes} bytes]"
                        )
                        last_line = line_no
                    hit_byte_limit = True
                    break

                selected.append(chunk)
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
