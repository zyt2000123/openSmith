from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from common.config import AGENT_DIR
from common.yaml_utils import YamlConfigError, load_yaml
from engine.mcp.client import MCPClient
from engine.mcp.config import mcp_server_log_summary, mcp_transport_from_config

from ..schemas.mcp import McpServerOut, McpToolSummaryOut

_MAX_CONCURRENT_DISCOVERIES = 4
_DISCOVERY_TIMEOUT_SECONDS = 35.0


class McpService:
    """Read configured MCP servers using the standard initialize/tools/list flow."""

    async def list_servers(self) -> list[McpServerOut]:
        try:
            profile = load_yaml(AGENT_DIR / "config.yaml")
        except YamlConfigError as exc:
            return [McpServerOut(name="config", type="unknown", status="error", error=str(exc))]

        configured = profile.get("mcp_servers", [])
        if not isinstance(configured, list):
            return [McpServerOut(name="config", type="unknown", status="error", error="mcp_servers must be a list")]

        result: list[McpServerOut | None] = [None] * len(configured)
        inspections: list[tuple[int, dict[str, Any]]] = []
        for index, raw in enumerate(configured):
            if not isinstance(raw, dict):
                result[index] = McpServerOut(
                    name=f"server-{index + 1}",
                    type="unknown",
                    status="error",
                    error="server entry must be a mapping",
                )
                continue
            inspections.append((index, raw))

        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_DISCOVERIES)

        async def inspect_limited(config: dict[str, Any], index: int) -> McpServerOut:
            async with semaphore:
                return await self._inspect_server(config, index)

        inspected = await asyncio.gather(
            *(inspect_limited(raw, index) for index, raw in inspections)
        )
        for (index, _), server in zip(inspections, inspected):
            result[index] = server
        return [server for server in result if server is not None]

    async def _inspect_server(self, config: dict[str, Any], index: int) -> McpServerOut:
        summary = mcp_server_log_summary(config)
        name_value = config.get("name") or config.get("alias") or f"server-{index + 1}"
        name = str(name_value)
        transport_type = str(config.get("type") or ("streamable_http" if config.get("url") else "stdio"))
        raw_url = summary.get("url") if isinstance(summary.get("url"), str) else None
        safe_url = None
        if raw_url:
            parsed_url = urlsplit(raw_url)
            safe_url = urlunsplit((parsed_url.scheme, parsed_url.netloc, parsed_url.path, "", ""))
        raw_command = summary.get("command") if isinstance(summary.get("command"), list) else []
        safe_command = [str(raw_command[0])] if raw_command else []
        common = {
            "name": name,
            "type": transport_type,
            "url": safe_url,
            "command": safe_command,
        }
        try:
            transport = mcp_transport_from_config(config)
            if transport is None:
                return McpServerOut(**common, status="error", error="invalid MCP transport configuration")
            client = MCPClient(transport=transport)
            try:
                try:
                    async with asyncio.timeout(_DISCOVERY_TIMEOUT_SECONDS):
                        await client.connect()
                        tools = await client.list_tools()
                except TimeoutError:
                    return McpServerOut(
                        **common,
                        status="error",
                        error=f"MCP discovery timed out after {_DISCOVERY_TIMEOUT_SECONDS:g} seconds",
                    )
            finally:
                await client.close()
            return McpServerOut(
                **common,
                status="connected",
                tools=[
                    McpToolSummaryOut(
                        name=tool.name,
                        description=tool.description,
                        inputSchema=tool.input_schema,
                    )
                    for tool in tools
                ],
            )
        except Exception as exc:
            return McpServerOut(**common, status="error", error=str(exc))
