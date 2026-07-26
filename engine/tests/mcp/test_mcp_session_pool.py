from __future__ import annotations

import asyncio

from engine.mcp.client import MCPTool
from engine.mcp.session_pool import MCPClientSessionPool


class FakeClient:
    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_session_pool_reuses_discovered_tools_and_closes_on_release() -> None:
    async def run():
        created: list[FakeClient] = []

        async def connect(config: dict):
            client = FakeClient(str(config["name"]))
            created.append(client)
            return client, [MCPTool("search", "search docs", {"type": "object"})]

        pool = MCPClientSessionPool(connect_server=connect)
        config = [{"type": "stdio", "name": "docs", "command": ["docs-server"]}]

        first = await pool.acquire("session-1", config)
        second = await pool.acquire("session-1", config)
        await pool.release("session-1")

        return created, first, second

    created, first, second = asyncio.run(run())

    assert len(created) == 1
    assert first[0].client is second[0].client
    assert first[0].tools[0].name == "search"
    assert created[0].closed is True


def test_session_pool_does_not_retain_inactive_session_locks() -> None:
    async def run() -> int:
        async def connect(config: dict):
            return FakeClient(str(config["name"])), []

        pool = MCPClientSessionPool(connect_server=connect)
        await pool.acquire(
            "finished-session", [{"type": "stdio", "name": "docs", "command": ["docs"]}],
        )
        await pool.release("finished-session")
        return len(pool._session_locks)

    assert asyncio.run(run()) == 0


def test_session_pool_does_not_block_another_session_during_connect() -> None:
    async def run():
        slow_connect_started = asyncio.Event()
        allow_slow_connect = asyncio.Event()
        created: list[FakeClient] = []

        async def connect(config: dict):
            if config["name"] == "slow":
                slow_connect_started.set()
                await allow_slow_connect.wait()
            client = FakeClient(str(config["name"]))
            created.append(client)
            return client, [MCPTool("search", "", {})]

        pool = MCPClientSessionPool(connect_server=connect)
        slow_task = asyncio.create_task(pool.acquire(
            "slow-session", [{"type": "stdio", "name": "slow", "command": ["slow"]}],
        ))
        try:
            await slow_connect_started.wait()
            fast_servers = await asyncio.wait_for(
                pool.acquire(
                    "fast-session", [{"type": "stdio", "name": "fast", "command": ["fast"]}],
                ),
                timeout=0.5,
            )
            return fast_servers, created
        finally:
            allow_slow_connect.set()
            await slow_task
            await pool.close()

    fast_servers, created = asyncio.run(run())

    assert fast_servers[0].client.name == "fast"
    assert [client.name for client in created] == ["fast", "slow"]


def test_session_pool_close_waits_for_an_inflight_connect() -> None:
    async def run():
        connect_started = asyncio.Event()
        allow_connect = asyncio.Event()
        created: list[FakeClient] = []

        async def connect(config: dict):
            connect_started.set()
            await allow_connect.wait()
            client = FakeClient(str(config["name"]))
            created.append(client)
            return client, [MCPTool("search", "", {})]

        pool = MCPClientSessionPool(connect_server=connect)
        acquire_task = asyncio.create_task(pool.acquire(
            "session-1", [{"type": "stdio", "name": "docs", "command": ["docs"]}],
        ))
        await connect_started.wait()
        close_task = asyncio.create_task(pool.close())
        await asyncio.sleep(0)
        close_waited = not close_task.done()
        allow_connect.set()
        servers = await acquire_task
        await close_task
        return close_waited, servers, created

    close_waited, servers, created = asyncio.run(run())

    assert close_waited is True
    assert servers[0].client.closed is True
    assert created[0].closed is True


def test_session_pool_replaces_connections_when_server_config_changes() -> None:
    async def run():
        created: list[FakeClient] = []

        async def connect(config: dict):
            client = FakeClient(str(config["name"]))
            created.append(client)
            return client, [MCPTool("search", "", {})]

        pool = MCPClientSessionPool(connect_server=connect)
        original = [{"type": "stdio", "name": "docs", "command": ["v1"]}]
        changed = [{"type": "stdio", "name": "docs", "command": ["v2"]}]

        first = await pool.acquire("session-1", original)
        second = await pool.acquire("session-1", changed)
        await pool.close()
        return created, first, second

    created, first, second = asyncio.run(run())

    assert len(created) == 2
    assert first[0].client is not second[0].client
    assert created[0].closed is True
    assert created[1].closed is True
