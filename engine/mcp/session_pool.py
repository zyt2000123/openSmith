"""Session-scoped ownership and reuse of MCP client connections."""

from __future__ import annotations

import asyncio
import json
import logging
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from engine.mcp.client import MCPClient, MCPConnectError, MCPTool
from engine.mcp.config import mcp_server_log_summary, mcp_tool_prefix_from_config, mcp_transport_from_config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionMCPServer:
    """One connected MCP server plus its discovery snapshot."""

    client: MCPClient
    prefix: str
    tools: list[MCPTool]


@dataclass
class _SessionEntry:
    fingerprint: str
    servers: list[SessionMCPServer]


ConnectServer = Callable[[dict[str, Any]], Awaitable[tuple[MCPClient, list[MCPTool]]]]


class MCPClientSessionPool:
    """Keep MCP connections alive for one conversation session.

    A runtime receives borrowed clients from this pool; it must never close
    them.  Session deletion and process shutdown own cleanup instead.
    """

    def __init__(self, *, connect_server: ConnectServer | None = None) -> None:
        self._connect_server = connect_server or _connect_server
        self._entries: dict[str, _SessionEntry] = {}
        self._lock = asyncio.Lock()
        # Coordinate shutdown with the point at which an operation joins a
        # session.  It is released before any network I/O, so it cannot turn a
        # slow MCP server into a process-wide bottleneck.
        self._lifecycle_lock = asyncio.Lock()
        # Session operations must remain serialized so that a reconnect cannot
        # race a release or replace a newer configuration.  The locks are
        # intentionally separate from ``_lock``: connecting to a remote MCP
        # server can take seconds and must not stall unrelated sessions.
        # Locks must stay alive while an acquire/release task holds one, but a
        # long-lived server must not retain a lock for every deleted session.
        self._session_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    async def acquire(
        self,
        session_id: str,
        configured_servers: list[dict[str, Any]],
    ) -> list[SessionMCPServer]:
        """Return cached servers or replace them when configuration changed."""
        fingerprint = _fingerprint(configured_servers)
        stale: list[SessionMCPServer] = []
        async with self._lifecycle_lock:
            session_lock = await self._get_session_lock(session_id)
        async with session_lock:
            async with self._lock:
                current = self._entries.get(session_id)
                if current is not None and current.fingerprint == fingerprint:
                    return current.servers

            servers = await self._connect_all(configured_servers)

            async with self._lock:
                # The per-session lock keeps this replacement ordered with
                # acquire/release calls for the same conversation.
                current = self._entries.get(session_id)
                if current is not None:
                    stale = current.servers
                self._entries[session_id] = _SessionEntry(fingerprint, servers)

            await _close_servers(stale)
            return servers

    async def release(self, session_id: str) -> None:
        """Close all MCP connections owned by a deleted conversation."""
        await self.evict(session_id)

    async def evict(self, session_id: str) -> None:
        """Drop a session's cached connections after a transport or tool-call
        failure so the next acquire reconnects instead of reusing a dead server.

        Unlike ``release()`` -- which is session teardown -- ``evict()`` keeps
        the session alive: the next ``acquire()`` with the same configuration
        reconnects every server instead of returning the now-untrusted cached
        clients.  Callers use this when a borrowed client raises a fatal error
        (stdio crash, HTTP session expiry, network failure).
        """
        async with self._lifecycle_lock:
            session_lock = await self._get_session_lock(session_id)
        async with session_lock:
            async with self._lock:
                entry = self._entries.pop(session_id, None)
            if entry is not None:
                await _close_servers(entry.servers)

    async def close(self) -> None:
        """Close every session connection during process shutdown."""
        entries: list[_SessionEntry] = []
        async with self._lifecycle_lock:
            async with self._lock:
                session_locks = list(self._session_locks.values())

            held_locks: list[asyncio.Lock] = []
            try:
                for session_lock in session_locks:
                    await session_lock.acquire()
                    held_locks.append(session_lock)
                async with self._lock:
                    entries = list(self._entries.values())
                    self._entries.clear()
            finally:
                for session_lock in reversed(held_locks):
                    session_lock.release()
        for entry in entries:
            await _close_servers(entry.servers)

    async def _connect_all(self, configured_servers: list[dict[str, Any]]) -> list[SessionMCPServer]:
        servers: list[SessionMCPServer] = []
        failures = 0
        try:
            for config in configured_servers:
                try:
                    client, tools = await self._connect_server(config)
                    servers.append(SessionMCPServer(
                        client=client,
                        prefix=mcp_tool_prefix_from_config(config),
                        tools=tools,
                    ))
                except Exception:
                    failures += 1
                    logger.exception(
                        "failed to connect session MCP server: %r",
                        mcp_server_log_summary(config),
                    )
        except BaseException:
            await _close_servers(servers)
            raise
        if configured_servers and failures == len(configured_servers):
            # Never cache an empty server list under a fingerprint whose
            # connect failed: that would silently remove every MCP tool for
            # the session while still short-circuiting the retry on the next
            # acquire.  Surface a typed error the caller can act on instead.
            raise MCPConnectError(
                f"all {len(configured_servers)} MCP servers failed to connect"
            )
        return servers

    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        """Return the stable lock that serializes one session's lifecycle."""
        async with self._lock:
            return self._session_locks.setdefault(session_id, asyncio.Lock())


async def _connect_server(config: dict[str, Any]) -> tuple[MCPClient, list[MCPTool]]:
    transport = mcp_transport_from_config(config)
    if transport is None:
        raise ValueError("invalid MCP transport configuration")
    client = MCPClient(transport=transport)
    try:
        await client.connect()
        return client, await client.list_tools()
    except BaseException:
        await client.close()
        raise


async def _close_servers(servers: list[SessionMCPServer]) -> None:
    for server in reversed(servers):
        try:
            await server.client.close()
        except Exception:
            logger.warning("failed to close session MCP client", exc_info=True)


def _fingerprint(configured_servers: list[dict[str, Any]]) -> str:
    """Create an in-memory stable key; it is never logged or persisted."""
    return json.dumps(configured_servers, sort_keys=True, separators=(",", ":"), default=str)
