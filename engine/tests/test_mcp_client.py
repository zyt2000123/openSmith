from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import httpx
import pytest

from engine.mcp import client as mcp_client
from engine.tool.interface import ToolCall
from engine.mcp.config import (
    mcp_server_log_summary as _mcp_server_log_summary,
    mcp_tool_prefix_from_config as _mcp_tool_prefix_from_config,
    mcp_transport_from_config as _mcp_transport_from_config,
)
from engine.mcp.client import (
    MAX_TOOL_NAME_LENGTH,
    MCPClient,
    MCPTool,
    StdioMCPTransport,
    StreamableHTTPMCPTransport,
    register_mcp_tools,
    register_mcp_tools_with_prefix,
)
from engine.tool.registry import ToolRegistry
from engine.safety.tool_guard import ToolGuard


SERVER = r'''
import json
import sys

initialized = False


def send(payload):
    print(json.dumps(payload), flush=True)


for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")

    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "method": "notifications/message",
            "params": {"level": "info", "data": "warming up"},
        })
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}},
        })
        continue

    if method == "notifications/initialized":
        initialized = True
        continue

    if method == "tools/list":
        if not initialized:
            send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32002, "message": "not initialized"}})
            continue
        cursor = message.get("params", {}).get("cursor")
        if cursor:
            send({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "tools": [
                        {
                            "name": "bad",
                            "description": "bad tool",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ]
                },
            })
        else:
            send({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "tools": [
                        {
                            "name": "ok",
                            "description": "ok tool",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ],
                    "nextCursor": "page-2",
                },
            })
        continue

    if method == "tools/call":
        name = message.get("params", {}).get("name")
        if name == "bad":
            send({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": "plain MCP failure"}],
                },
            })
        else:
            send({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": "ok result"}],
                },
            })
'''


async def _new_client(tmp: Path) -> MCPClient:
    server = tmp / "server.py"
    server.write_text(SERVER, encoding="utf-8")
    client = MCPClient([sys.executable, str(server)])
    await client.connect()
    return client


def test_mcp_client_sends_initialized_skips_notifications_and_pages_tools():
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            client = await _new_client(Path(tmp))
            try:
                tools = await client.list_tools()
                return [tool.name for tool in tools]
            finally:
                await client.close()

    assert asyncio.run(run()) == ["ok", "bad"]


def test_mcp_client_rejects_repeated_tool_list_cursor():
    class RepeatingCursorTransport:
        label = "repeating-cursor"

        def __init__(self) -> None:
            self.calls = 0

        async def connect(self):
            pass

        async def send_request(self, method, params):
            assert method == "tools/list"
            self.calls += 1
            await asyncio.sleep(0)
            return {"tools": [], "nextCursor": "repeat"}

        async def send_notification(self, method, params):
            pass

        async def close(self):
            pass

    async def run():
        transport = RepeatingCursorTransport()
        client = MCPClient(transport=transport)
        with pytest.raises(RuntimeError, match="repeated cursor"):
            await asyncio.wait_for(client.list_tools(), timeout=0.1)
        return transport.calls

    assert asyncio.run(run()) == 2


def test_mcp_client_list_tools_skips_malformed_entries():
    """Tools missing a valid string ``name`` must be skipped, not crash list_tools."""
    class MixedToolsTransport:
        label = "mixed-tools"

        async def connect(self):
            pass

        async def send_request(self, method, params):
            assert method == "tools/list"
            return {"tools": [
                {"name": "valid", "description": "ok", "inputSchema": {}},
                {"description": "no name key"},
                {"name": None, "description": "null name"},
                {"name": 123, "description": "non-string name"},
                {"name": "", "description": "empty name"},
                "not-a-dict",
                None,
            ]}

        async def send_notification(self, method, params):
            pass

        async def close(self):
            pass

    async def run():
        transport = MixedToolsTransport()
        client = MCPClient(transport=transport)
        tools = await client.list_tools()
        await client.close()
        return [tool.name for tool in tools]

    assert asyncio.run(run()) == ["valid"]


def test_mcp_client_limits_tool_list_pages(monkeypatch):
    monkeypatch.setattr(mcp_client, "MAX_MCP_TOOL_LIST_PAGES", 2)

    class EndlessCursorTransport:
        label = "endless-cursor"

        def __init__(self) -> None:
            self.calls = 0

        async def connect(self):
            pass

        async def send_request(self, method, params):
            assert method == "tools/list"
            self.calls += 1
            return {"tools": [], "nextCursor": f"page-{self.calls}"}

        async def send_notification(self, method, params):
            pass

        async def close(self):
            pass

    async def run():
        transport = EndlessCursorTransport()
        client = MCPClient(transport=transport)
        with pytest.raises(RuntimeError, match="maximum page limit"):
            await client.list_tools()
        return transport.calls

    assert asyncio.run(run()) == 2


def test_mcp_tool_is_error_becomes_registry_error():
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            client = await _new_client(Path(tmp))
            registry = ToolRegistry()
            try:
                await register_mcp_tools(registry, client)
                return await registry.execute(ToolCall(id="call-1", name="mcp_bad", arguments={}))
            finally:
                await client.close()

    result = asyncio.run(run())
    assert result.is_error
    assert result.content == "plain MCP failure"


def test_mcp_registration_skips_bad_tool_and_keeps_good_tool():
    class FakeClient:
        _command = ["fake-mcp"]

        async def list_tools(self):
            return [
                MCPTool("dup", "", {}),
                MCPTool("kept", "", {}),
            ]

        async def call_tool(self, name, arguments):
            return name

    async def run():
        registry = ToolRegistry()
        registry.register("mcp_dup", "", {}, lambda: "existing")
        return await register_mcp_tools(registry, FakeClient())

    assert asyncio.run(run()) == 1


def test_streamable_http_transport_handles_session_headers_and_json_responses():
    seen_headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        payload = json.loads(request.content.decode())
        method = payload.get("method")
        request_id = payload.get("id")

        if method == "initialize":
            return httpx.Response(
                200,
                headers={"content-type": "application/json", "MCP-Session-Id": "session-1"},
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}},
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "tools": [
                            {
                                "name": "lookup",
                                "description": "lookup tool",
                                "inputSchema": {"type": "object", "properties": {}},
                            }
                        ]
                    },
                },
            )
        if method == "tools/call":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"content": [{"type": "text", "text": "looked up"}]},
                },
            )
        raise AssertionError(method)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            transport = StreamableHTTPMCPTransport(
                "https://mcp.example.test/mcp",
                headers={"Authorization": "Bearer token"},
                http_client=http_client,
            )
            client = MCPClient(transport=transport)
            await client.connect()
            tools = await client.list_tools()
            result = await client.call_tool("lookup", {})
            await client.close()
            return [tool.name for tool in tools], result

    tools, result = asyncio.run(run())

    assert tools == ["lookup"]
    assert result == "looked up"
    assert seen_headers[0]["accept"] == "application/json, text/event-stream"
    assert "mcp-session-id" not in seen_headers[0]
    assert seen_headers[2]["mcp-protocol-version"] == "2025-11-25"
    assert seen_headers[2]["mcp-session-id"] == "session-1"
    assert seen_headers[2]["authorization"] == "Bearer token"


def test_streamable_http_transport_accepts_sse_request_response():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        request_id = payload.get("id")
        method = payload.get("method")
        if method == "initialize":
            body = (
                'event: message\n'
                'data: {"jsonrpc":"2.0","method":"notifications/message","params":{"level":"info"}}\n\n'
                f'data: {{"jsonrpc":"2.0","id":{request_id},"result":{{"protocolVersion":"2025-11-25"}}}}\n\n'
            )
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            body = (
                'data: {"jsonrpc":"2.0","method":"notifications/message","params":{"level":"debug"}}\n\n'
                f'data: {{"jsonrpc":"2.0","id":{request_id},"result":{{"tools":[]}}}}\n\n'
            )
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)
        raise AssertionError(method)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = MCPClient(
                transport=StreamableHTTPMCPTransport(
                    "https://mcp.example.test/mcp",
                    http_client=http_client,
                )
            )
            await client.connect()
            tools = await client.list_tools()
            await client.close()
            return tools

    assert asyncio.run(run()) == []


def test_streamable_http_transport_rejects_oversized_json_response():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        request_id = payload.get("id")
        if payload.get("method") == "initialize":
            return httpx.Response(200, json={
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"protocolVersion": "2025-11-25"},
            })
        if payload.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b"x" * (1024 * 1024 + 1),
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = MCPClient(transport=StreamableHTTPMCPTransport(
                "https://mcp.example.test/mcp", http_client=http_client,
            ))
            await client.connect()
            try:
                await client.list_tools()
            finally:
                await client.close()

    with pytest.raises(RuntimeError, match="exceeds maximum size"):
        asyncio.run(run())


def test_streamable_http_transport_rejects_oversized_notification_response():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        request_id = payload.get("id")
        if payload.get("method") == "initialize":
            return httpx.Response(200, json={
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"protocolVersion": "2025-11-25"},
            })
        return httpx.Response(202, content=b"x" * (1024 * 1024 + 1))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = MCPClient(transport=StreamableHTTPMCPTransport(
                "https://mcp.example.test/mcp", http_client=http_client,
            ))
            try:
                await client.connect()
            finally:
                await client.close()

    with pytest.raises(RuntimeError, match="exceeds maximum size"):
        asyncio.run(run())


def test_mcp_config_supports_stdio_and_streamable_http_transports():
    stdio = _mcp_transport_from_config({"type": "stdio", "command": [sys.executable, "-V"]})
    http = _mcp_transport_from_config({
        "type": "streamable_http",
        "url": "https://mcp.example.test/mcp",
        "headers": {"Authorization": "Bearer token"},
    })

    assert type(stdio).__name__ == "StdioMCPTransport"
    assert type(http).__name__ == "StreamableHTTPMCPTransport"
    assert _mcp_tool_prefix_from_config({"name": "github"}) == "mcp_github"
    assert _mcp_tool_prefix_from_config({}) == "mcp"


def test_mcp_registration_can_namespace_servers_to_avoid_collisions():
    class FakeClient:
        async def list_tools(self):
            return [MCPTool("search", "", {})]

        async def call_tool(self, name, arguments):
            return name

    async def run():
        registry = ToolRegistry()
        first = await register_mcp_tools_with_prefix(registry, FakeClient(), prefix="mcp_github")
        second = await register_mcp_tools_with_prefix(registry, FakeClient(), prefix="mcp_docs")
        return first, second, sorted(tool.name for tool in registry.list_tools())

    assert asyncio.run(run()) == (1, 1, ["mcp_docs_search", "mcp_github_search"])


def test_stdio_transport_merges_env_with_parent_environment():
    transport = StdioMCPTransport(["fake"], env={"ONLY_THIS": "value"})

    assert transport._env is not None
    assert transport._env["ONLY_THIS"] == "value"
    assert "PATH" in transport._env


def test_mcp_connect_rejects_unsupported_protocol_version():
    class BadVersionTransport:
        label = "bad-version"

        async def connect(self):
            pass

        async def send_request(self, method, params):
            return {"protocolVersion": "1900-01-01"}

        async def send_notification(self, method, params):
            raise AssertionError("initialized notification should not be sent")

        async def close(self):
            self.closed = True

    async def run():
        transport = BadVersionTransport()
        client = MCPClient(transport=transport)
        try:
            await client.connect()
        except RuntimeError as exc:
            return str(exc), getattr(transport, "closed", False)
        raise AssertionError("unsupported protocol version was accepted")

    message, closed = asyncio.run(run())

    assert "Unsupported MCP protocol version" in message
    assert closed


def test_mcp_connect_closes_transport_when_initialize_fails():
    class FailingInitializeTransport:
        label = "failing-initialize"

        def __init__(self):
            self.closed = False

        async def connect(self):
            pass

        async def send_request(self, method, params):
            raise RuntimeError("initialize failed")

        async def send_notification(self, method, params):
            raise AssertionError("initialized notification should not be sent")

        async def close(self):
            self.closed = True

    async def run():
        transport = FailingInitializeTransport()
        client = MCPClient(transport=transport)
        try:
            await client.connect()
        except RuntimeError as exc:
            return str(exc), transport.closed
        raise AssertionError("initialize failure was swallowed")

    message, closed = asyncio.run(run())

    assert message == "initialize failed"
    assert closed


def test_mcp_registration_rejects_non_ascii_tool_names():
    class FakeClient:
        async def list_tools(self):
            return [MCPTool("搜索", "", {}), MCPTool("safe-tool", "", {})]

        async def call_tool(self, name, arguments):
            return name

    async def run():
        registry = ToolRegistry()
        count = await register_mcp_tools_with_prefix(registry, FakeClient(), prefix="mcp_docs")
        return count, [tool.name for tool in registry.list_tools()]

    assert asyncio.run(run()) == (1, ["mcp_docs_safe_tool"])


def test_registered_mcp_tools_always_require_approval():
    class FakeClient:
        async def list_tools(self):
            return [MCPTool("mutate_remote", "unknown remote operation", {})]

        async def call_tool(self, name, arguments):
            return name

    async def run():
        registry = ToolRegistry()
        await register_mcp_tools(registry, FakeClient())
        definition = registry.get("mcp_mutate_remote")
        result = ToolGuard(
            Path("missing-rules.json"), tool_registry=registry.definitions(),
        ).check(ToolCall("call", "mcp_mutate_remote", {}))
        return definition, result

    definition, result = asyncio.run(run())
    assert definition is not None
    assert definition.side_effect == "external"
    assert definition.concurrency == "serial"
    assert result.approval_required


def test_mcp_openai_schema_helper_sanitizes_tool_names():
    schemas = MCPClient([sys.executable, "-V"]).to_openai_schemas([
        MCPTool("safe-tool", "", {}),
        MCPTool("搜索", "", {}),
    ])

    assert [schema["function"]["name"] for schema in schemas] == ["mcp_safe_tool"]


def test_mcp_server_log_summary_redacts_secret_values():
    summary = _mcp_server_log_summary({
        "type": "streamable_http",
        "name": "github",
        "url": "https://example.test/mcp",
        "headers": {"Authorization": "Bearer secret-token"},
        "env": {"GITHUB_TOKEN": "ghp_secret"},
    })

    assert summary == {
        "type": "streamable_http",
        "name": "github",
        "url": "https://example.test/mcp",
        "headers": ["Authorization"],
        "env": ["GITHUB_TOKEN"],
    }
    assert "secret-token" not in repr(summary)
    assert "ghp_secret" not in repr(summary)


def test_mcp_tool_names_are_capped_with_stable_hash_suffix():
    long_name = "x" * 120

    schemas = MCPClient([sys.executable, "-V"]).to_openai_schemas([MCPTool(long_name, "", {})])
    schema_name = schemas[0]["function"]["name"]

    assert len(schema_name.removeprefix("mcp_")) == MAX_TOOL_NAME_LENGTH
    assert len(schema_name) <= MAX_TOOL_NAME_LENGTH + len("mcp_")
    assert schema_name == MCPClient([sys.executable, "-V"]).to_openai_schemas(
        [MCPTool(long_name, "", {})]
    )[0]["function"]["name"]


def test_stdio_transport_serializes_concurrent_requests():
    """Concurrent tool calls over one stdio pipe must not steal each
    other's responses (regression: interleaved reads dropped replies)."""
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            client = await _new_client(Path(tmp))
            try:
                results = await asyncio.gather(
                    *[client.call_tool("ok", {}) for _ in range(5)]
                )
                return results
            finally:
                await client.close()

    assert asyncio.run(run()) == ["ok result"] * 5


def test_stdio_transport_drains_server_stderr_before_response():
    """A noisy MCP server must not block on its stderr pipe before replying."""
    noisy_server = r'''
import json
import sys

for line in sys.stdin:
    message = json.loads(line)
    request_id = message.get("id")
    if request_id is None:
        continue
    sys.stderr.write("x" * (1024 * 1024))
    sys.stderr.flush()
    print(json.dumps({
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}},
    }), flush=True)
'''

    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            server = Path(tmp) / "noisy_server.py"
            server.write_text(noisy_server, encoding="utf-8")
            client = MCPClient([sys.executable, str(server)])
            try:
                await asyncio.wait_for(client.connect(), timeout=2)
            finally:
                await client.close()

    asyncio.run(run())


def test_stdio_transport_waits_after_killing_timed_out_process():
    class FakeProcess:
        stdin = None

        def __init__(self):
            self.calls = 0
            self.killed = False

        async def wait(self):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(10)
            return 0

        def kill(self):
            self.killed = True

    async def run():
        transport = StdioMCPTransport(["fake"], close_timeout=0.01)
        process = FakeProcess()
        transport._process = process
        await transport.close()
        return process.killed, process.calls

    assert asyncio.run(run()) == (True, 2)


def test_stdio_transport_cancellation_kills_and_reaps_process():
    class FakeProcess:
        stdin = None

        def __init__(self):
            self.returncode = None
            self.killed = False
            self.wait_started = asyncio.Event()
            self.release = asyncio.Event()

        async def wait(self):
            self.wait_started.set()
            await self.release.wait()
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9
            self.release.set()

    async def run():
        transport = StdioMCPTransport(["fake"])
        process = FakeProcess()
        transport._process = process
        closing = asyncio.create_task(transport.close())
        await process.wait_started.wait()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        return process.killed, transport._process

    assert asyncio.run(run()) == (True, None)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    raise SystemExit(1 if failures else 0)
