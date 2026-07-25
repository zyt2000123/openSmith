"""MCP server configuration parsing and registration helpers.

Bridges agent profile configuration (``mcp_servers`` entries) to the
transport and client implementations in ``engine.mcp.client``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.execution.orchestration.runtime import RuntimeContext, RuntimeServices

logger = logging.getLogger(__name__)


async def register_mcp_tools(
    profile_config: dict,
    runtime: "RuntimeContext",
    services: "RuntimeServices",
) -> None:
    """Register MCP tools from the agent's profile configuration.

    Iterates ``profile_config["mcp_servers"]`` and connects each server,
    isolating failures so one broken server cannot prevent the rest from
    registering.
    """
    mcp_servers = profile_config.get("mcp_servers", [])
    if not isinstance(mcp_servers, list) or not mcp_servers:
        return
    valid_servers = [server for server in mcp_servers if isinstance(server, dict)]
    session_pool = services.mcp_session_pool if runtime.session_id else None
    if session_pool is not None:
        try:
            servers = await session_pool.acquire(runtime.session_id, valid_servers)
            services.mcp_clients.extend(server.client for server in servers)
            from engine.mcp.client import register_mcp_tools_with_prefix
            for server in servers:
                await register_mcp_tools_with_prefix(
                    services.tool_registry,
                    server.client,
                    prefix=server.prefix,
                    tools=server.tools,
                )
        except Exception:
            logger.exception("failed to register session MCP tools (agent=%s)", runtime.agent_id)
        return
    try:
        from engine.mcp.client import (
            MCPClient,
            register_mcp_tools_with_prefix,
        )
    except Exception:
        logger.exception("failed to import MCP client (agent=%s)", runtime.agent_id)
        return
    for srv in valid_servers:
        try:
            transport = mcp_transport_from_config(srv)
            if transport is None:
                continue
            prefix = mcp_tool_prefix_from_config(srv)
            client = MCPClient(transport=transport)
            await client.connect()
            services.mcp_clients.append(client)
            await register_mcp_tools_with_prefix(services.tool_registry, client, prefix=prefix)
        except Exception:
            logger.exception(
                "failed to register MCP server (agent=%s, server=%r)",
                runtime.agent_id, mcp_server_log_summary(srv),
            )


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
    """Build a safe-to-log summary of an MCP server config (no secret values)."""
    summary: dict[str, object] = {}
    for key in ("type", "name", "alias", "url", "command", "timeout"):
        value = config.get(key)
        if value is not None:
            summary[key] = value
    if isinstance(config.get("headers"), dict):
        summary["headers"] = sorted(config["headers"].keys())
    if isinstance(config.get("env"), dict):
        summary["env"] = sorted(config["env"].keys())
    return summary
