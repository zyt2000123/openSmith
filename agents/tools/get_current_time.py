"""Read the local machine clock without putting volatile time in the system prompt."""
# 将易过期的当前时间延迟到确有需要时才读取，避免污染稳定提示词缓存。

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone as utc_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TOOL_META = {
    "name": "get_current_time",
    "description": (
        "Get the current machine time when an answer depends on the present date or time. "
        "Optionally convert it to an IANA timezone such as Asia/Shanghai."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "Optional IANA timezone, for example Asia/Shanghai.",
            },
        },
        "required": [],
    },
    "permission_level": "read",
    "approval_policy": "never",
    "side_effect": "none",
    "execution_environment": "host",
}


def _utc_offset(value: datetime) -> str:
    offset = value.utcoffset()
    if offset is None:
        return "+00:00"
    seconds = int(offset.total_seconds())
    sign = "+" if seconds >= 0 else "-"
    hours, remainder = divmod(abs(seconds), 3600)
    return f"{sign}{hours:02d}:{remainder // 60:02d}"


def _execute_sync(*, timezone: str | None = None) -> str:
    if timezone is not None and (not isinstance(timezone, str) or not timezone.strip()):
        return "Error: timezone must be a non-empty IANA timezone string"

    if timezone:
        try:
            zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            return f"Error: unknown IANA timezone: {timezone}"
        except ValueError:
            # zoneinfo rejects absolute keys and keys escaping the tzdata root
            # with ValueError, not ZoneInfoNotFoundError; letting it escape
            # turned a bad argument into an opaque provider exception.
            return f"Error: invalid IANA timezone key: {timezone}"
        reported_timezone = timezone
    else:
        local = datetime.now().astimezone()
        zone = local.tzinfo
        reported_timezone = local.tzname() or "local"

    local_time = datetime.now(zone)
    return json.dumps({
        "source": "machine_clock",
        "local_time": local_time.isoformat(),
        "timezone": reported_timezone,
        "utc_offset": _utc_offset(local_time),
        "utc_time": datetime.now(utc_timezone.utc).isoformat(),
    }, ensure_ascii=False, separators=(",", ":"))


async def execute(*, timezone: str | None = None) -> str:
    return await asyncio.to_thread(_execute_sync, timezone=timezone)
