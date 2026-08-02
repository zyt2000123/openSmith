from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services import mcp_service as mcp_service_module
from app.services.mcp_service import McpService


@pytest.mark.asyncio
async def test_mcp_service_discovers_standard_tools_and_keeps_input_schema_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "config.yaml").write_text(
        """
mcp_servers:
  - name: demo
    type: streamable_http
    url: https://mcp.example/sse
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(mcp_service_module, "AGENT_DIR", agent_dir)

    class FakeTransport:
        label = "demo"

        async def connect(self):
            pass

        async def send_request(self, method, params):
            return {}

        async def send_notification(self, method, params):
            pass

        async def close(self):
            pass

    class FakeTool:
        name = "search"
        description = "Search documents"
        input_schema = {"type": "object", "properties": {"query": {"type": "string"}}}

    class FakeClient:
        def __init__(self, *, transport):
            self.transport = transport

        async def connect(self):
            pass

        async def list_tools(self):
            return [FakeTool()]

        async def close(self):
            pass

    monkeypatch.setattr(mcp_service_module, "MCPClient", FakeClient)
    monkeypatch.setattr(mcp_service_module, "mcp_transport_from_config", lambda config: FakeTransport())

    result = await McpService().list_servers()

    assert result[0].status == "connected"
    assert result[0].tools[0].name == "search"
    assert result[0].model_dump(by_alias=True)["tools"][0]["inputSchema"]["type"] == "object"


@pytest.mark.asyncio
async def test_mcp_service_probes_configured_servers_concurrently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "config.yaml").write_text(
        """
mcp_servers:
  - name: slow
    command: [slow]
  - name: fast
    command: [fast]
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(mcp_service_module, "AGENT_DIR", agent_dir)

    slow_started = asyncio.Event()
    allow_slow = asyncio.Event()
    fast_finished = asyncio.Event()

    class FakeClient:
        def __init__(self, *, transport):
            self.name = transport["name"]

        async def connect(self):
            if self.name == "slow":
                slow_started.set()
                await allow_slow.wait()

        async def list_tools(self):
            if self.name == "fast":
                fast_finished.set()
            return []

        async def close(self):
            pass

    monkeypatch.setattr(mcp_service_module, "MCPClient", FakeClient)
    monkeypatch.setattr(mcp_service_module, "mcp_transport_from_config", lambda config: config)

    task = asyncio.create_task(McpService().list_servers())
    try:
        await asyncio.wait_for(slow_started.wait(), timeout=0.1)
        await asyncio.wait_for(fast_finished.wait(), timeout=0.1)
    finally:
        allow_slow.set()

    result = await task
    assert [item.name for item in result] == ["slow", "fast"]


@pytest.mark.asyncio
async def test_mcp_service_reports_a_discovery_timeout_and_closes_the_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "config.yaml").write_text(
        "mcp_servers:\n  - name: stalled\n    command: [stalled]",
        encoding="utf-8",
    )
    monkeypatch.setattr(mcp_service_module, "AGENT_DIR", agent_dir)
    monkeypatch.setattr(mcp_service_module, "_DISCOVERY_TIMEOUT_SECONDS", 0.01)
    closed = False

    class FakeClient:
        def __init__(self, *, transport):
            pass

        async def connect(self):
            await asyncio.Event().wait()

        async def list_tools(self):
            return []

        async def close(self):
            nonlocal closed
            closed = True

    monkeypatch.setattr(mcp_service_module, "MCPClient", FakeClient)
    monkeypatch.setattr(mcp_service_module, "mcp_transport_from_config", lambda config: config)

    result = await McpService().list_servers()

    assert result[0].status == "error"
    assert "timed out" in (result[0].error or "")
    assert closed is True
