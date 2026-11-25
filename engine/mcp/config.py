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
    vault_store: Any | None = None,
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

            async def _evict_dead_session() -> None:
                """Reconnect the next acquire instead of reusing a dead client."""
                await session_pool.evict(session_id)

            for server in servers:
                srv_cfg = next(
                    (
                        s for s in valid_servers
                        if mcp_tool_prefix_from_config(s) == server.prefix
                    ),
                    {},
                )
                registered_tools += await register_mcp_tools_with_prefix(
                    tool_registry,
                    server.client,
                    prefix=server.prefix,
                    tools=server.tools,
                    on_connection_failure=_evict_dead_session,
                    reads_private_data=bool(srv_cfg.get("reads_private_data", False)),
                    egress=bool(srv_cfg.get("egress", True)),
                    accepts_vault_placeholders=bool(
                        srv_cfg.get("accepts_vault_placeholders", True)
                    ),
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
            transport = mcp_transport_from_config(srv, vault_store=vault_store)
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
                reads_private_data=bool(srv.get("reads_private_data", False)),
                egress=bool(srv.get("egress", True)),
                accepts_vault_placeholders=bool(
                    srv.get("accepts_vault_placeholders", True)
                ),
            )
        except Exception:
            logger.exception(
                "failed to register MCP server (agent=%s, server=%r)",
                agent_id, mcp_server_log_summary(srv),
            )
    return MCPRegistration(tuple(clients), registered_tools)


def _resolve_mcp_vault_mapping(
    values: dict | None,
    vault_store: Any | None,
    *,
    tool_prefix: str,
    target_domain: str | None = None,
) -> dict[str, str] | None:
    if not isinstance(values, dict):
        return None
    if vault_store is None:
        return values
    resolved: dict[str, str] = {}
    for k, v in values.items():
        if isinstance(v, str) and "{{vault:" in v:
            resolved_val, _ = vault_store.resolve_string(
                v, tool_name=f"{tool_prefix}_*", target_domain=target_domain
            )
            resolved[str(k)] = resolved_val
        else:
            resolved[str(k)] = v
    return resolved


def mcp_transport_from_config(config: dict, *, vault_store: Any | None = None):
    """Build an MCP transport object from a server config dict.

    Resolves ``{{vault:NAME}}`` placeholders in ``headers``, ``env``, and ``url``
    at transport construction time when a ``vault_store`` is available.
    """
    from urllib.parse import urlparse

    from common.vault import VaultStore, extract_vault_placeholders
    from engine.mcp.client import StdioMCPTransport, StreamableHTTPMCPTransport

    transport_type = str(config.get("type") or "").strip().lower().replace("-", "_")
    if not transport_type:
        transport_type = "streamable_http" if config.get("url") else "stdio"

    needs_vault = bool(extract_vault_placeholders(config))
    store = vault_store
    if needs_vault and store is None:
        store = VaultStore()
    prefix = mcp_tool_prefix_from_config(config)

    if transport_type == "stdio":
        command = config.get("command", [])
        if not isinstance(command, list) or not command:
            return None
        env = _resolve_mcp_vault_mapping(
            config.get("env") if isinstance(config.get("env"), dict) else None,
            store,
            tool_prefix=prefix,
        )
        return StdioMCPTransport(command, env=env)

    if transport_type in {"http", "streamable_http"}:
        url = config.get("url")
        if not isinstance(url, str) or not url:
            return None
        parsed_raw = urlparse(url)
        domain = parsed_raw.hostname.lower().rstrip(".") if parsed_raw.hostname else None
        if store is not None and "{{vault:" in url:
            url, _ = store.resolve_string(url, tool_name=f"{prefix}_*", target_domain=domain)
        headers = _resolve_mcp_vault_mapping(
            config.get("headers") if isinstance(config.get("headers"), dict) else None,
            store,
            tool_prefix=prefix,
            target_domain=domain,
        )
        timeout = config.get("timeout", 30.0)
        return StreamableHTTPMCPTransport(
            url,
            headers=headers,
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
