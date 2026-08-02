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
async def test_mcp_service_surfaces_a_hanging_server_as_an_error_not_a_block(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A server that never answers connect must be reported as an error, not hang
    the whole list_servers call."""
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "config.yaml").write_text(
        """
mcp_servers:
  - name: stuck
    type: stdio
    command: [echo, hello]
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(mcp_service_module, "AGENT_DIR", agent_dir)

    class FakeTransport:
        label = "stuck"

        async def connect(self):
            pass

        async def send_request(self, method, params):
            return {}

        async def send_notification(self, method, params):
            pass

        async def close(self):
            pass

    class FakeClient:
        def __init__(self, *, transport):
            self.transport = transport

        async def connect(self):
            await asyncio.sleep(60)  # hangs forever

        async def list_tools(self):
            return []

        async def close(self):
            pass

    monkeypatch.setattr(mcp_service_module, "MCPClient", FakeClient)
    monkeypatch.setattr(mcp_service_module, "mcp_transport_from_config", lambda config: FakeTransport())
    monkeypatch.setattr(mcp_service_module, "_MCP_CONNECT_TIMEOUT_SECONDS", 0.01)

    result = await McpService().list_servers()

    assert len(result) == 1
    assert result[0].status == "error"
    assert "timed out" in (result[0].error or "").lower()
