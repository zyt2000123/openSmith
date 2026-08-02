"""MCP server configuration parsing and registration helpers.

Bridges agent profile configuration (``mcp_servers`` entries) to the
transport and client implementations in ``engine.mcp.client``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from engine.tool.registry import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MCPRegistration:
    """Clients and tool count produced by one profile registration."""

    clients: tuple[Any, ...] = ()
    registered_tools: int = 0


async def register_configured_mcp_tools(
    profile_config: dict,
    *,
    session_id: str | None,
    agent_id: str,
    tool_registry: ToolRegistry,
    session_pool: Any | None = None,
) -> MCPRegistration:
    """Register MCP tools from the agent's profile configuration.

    Iterates ``profile_config["mcp_servers"]`` and connects each server,
    isolating failures so one broken server cannot prevent the rest from
    registering.  The caller receives the clients and applies its own
    ownership policy; this module never reaches into an execution container.
    """
    mcp_servers = profile_config.get("mcp_servers", [])
    if not isinstance(mcp_servers, list) or not mcp_servers:
        return MCPRegistration()
    valid_servers = [server for server in mcp_servers if isinstance(server, dict)]
    if session_pool is not None and session_id:
        try:
            servers = await session_pool.acquire(session_id, valid_servers)
            from engine.mcp.client import register_mcp_tools_with_prefix
            registered_tools = 0
            for server in servers:
                registered_tools += await register_mcp_tools_with_prefix(
                    tool_registry,
                    server.client,
                    prefix=server.prefix,
                    tools=server.tools,
                )
            return MCPRegistration(
                clients=tuple(server.client for server in servers),
                registered_tools=registered_tools,
            )
        except Exception:
            logger.exception("failed to register session MCP tools (agent=%s)", agent_id)
        return MCPRegistration()
    try:
        from engine.mcp.client import (
            MCPClient,
            register_mcp_tools_with_prefix,
        )
    except Exception:
        logger.exception("failed to import MCP client (agent=%s)", agent_id)
        return MCPRegistration()
    clients: list[Any] = []
    registered_tools = 0
    for srv in valid_servers:
        try:
            transport = mcp_transport_from_config(srv)
            if transport is None:
                continue
            prefix = mcp_tool_prefix_from_config(srv)
            client = MCPClient(transport=transport)
            await client.connect()
            clients.append(client)
            registered_tools += await register_mcp_tools_with_prefix(
                tool_registry,
                client,
                prefix=prefix,
            )
        except Exception:
            logger.exception(
                "failed to register MCP server (agent=%s, server=%r)",
                agent_id, mcp_server_log_summary(srv),
            )
    return MCPRegistration(tuple(clients), registered_tools)


def mcp_transport_from_config(config: dict):
    """Build an MCP transport object from a server config dict."""
    from engine.mcp.client import StdioMCPTransport, StreamableHTTPMCPTransport

    transport_type = str(config.get("type") or "").strip().lower().replace("-", "_")
    if not transport_type:
        transport_type = "streamable_http" if config.get("url") else "stdio"

    if transport_type == "stdio":
        command = config.get("command", [])
        if not isinstance(command, list) or not command:
            return None
        env = config.get("env")
        return StdioMCPTransport(command, env=env if isinstance(env, dict) else None)

    if transport_type in {"http", "streamable_http"}:
        url = config.get("url")
        if not isinstance(url, str) or not url:
            return None
        headers = config.get("headers")
        timeout = config.get("timeout", 30.0)
        return StreamableHTTPMCPTransport(
            url,
            headers=headers if isinstance(headers, dict) else None,
            timeout=float(timeout) if isinstance(timeout, (int, float)) else 30.0,
        )

    raise ValueError(f"unsupported MCP transport type: {transport_type}")


def mcp_tool_prefix_from_config(config: dict) -> str:
    """Derive the tool-name prefix for an MCP server."""
    name = config.get("name") or config.get("alias")
    if isinstance(name, str) and name:
        return f"mcp_{name}"
    return "mcp"


def mcp_server_log_summary(config: dict) -> dict[str, object]:
    """Build a safe-to-log summary of an MCP server config (no secret values).

    ``command`` reduces to its executable name and ``url`` strips any query
    string and embedded credentials: command arguments and URL query
    parameters routinely carry tokens that must never reach a log.
    """
    summary: dict[str, object] = {}
    for key in ("type", "name", "alias", "timeout"):
        value = config.get(key)
        if value is not None:
            summary[key] = value
    if isinstance(config.get("command"), list):
        from engine.mcp.client import _redact_command
        summary["command"] = _redact_command(config["command"])
    if isinstance(config.get("url"), str):
        from engine.mcp.client import _redact_url
        summary["url"] = _redact_url(config["url"])
    if isinstance(config.get("headers"), dict):
        summary["headers"] = sorted(config["headers"].keys())
    if isinstance(config.get("env"), dict):
        summary["env"] = sorted(config["env"].keys())
    return summary
