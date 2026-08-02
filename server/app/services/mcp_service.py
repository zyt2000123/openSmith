from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from common import config as common_config
from common.yaml_utils import YamlConfigError, load_yaml
from engine.mcp.client import MCPClient
from engine.mcp.config import mcp_server_log_summary, mcp_transport_from_config

from ..schemas.mcp import McpServerOut, McpToolSummaryOut

# Compatibility seam for tests embedding a temporary runtime profile.
AGENT_DIR: Path | None = None


def _agent_dir() -> Path:
    return AGENT_DIR if AGENT_DIR is not None else common_config.PATHS.agent_dir


class McpService:
    """Read configured MCP servers using the standard initialize/tools/list flow."""

    async def list_servers(self) -> list[McpServerOut]:
        try:
            profile = load_yaml(_agent_dir() / "config.yaml")
        except YamlConfigError as exc:
            return [McpServerOut(name="config", type="unknown", status="error", error=str(exc))]

        configured = profile.get("mcp_servers", [])
        if not isinstance(configured, list):
            return [McpServerOut(name="config", type="unknown", status="error", error="mcp_servers must be a list")]

        result: list[McpServerOut] = []
        for index, raw in enumerate(configured):
            if not isinstance(raw, dict):
                result.append(McpServerOut(
                    name=f"server-{index + 1}",
                    type="unknown",
                    status="error",
                    error="server entry must be a mapping",
                ))
                continue
            result.append(await self._inspect_server(raw, index))
        return result

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
                await client.connect()
                tools = await client.list_tools()
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
