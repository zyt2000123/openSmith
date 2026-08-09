from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from engine.mcp import client as mcp_client
from engine.tool.interface import ToolCall
from engine.mcp.config import (
    register_configured_mcp_tools,
    mcp_server_log_summary as _mcp_server_log_summary,
    mcp_tool_prefix_from_config as _mcp_tool_prefix_from_config,
    mcp_transport_from_config as _mcp_transport_from_config,
)
from engine.mcp.client import (
    MAX_TOOL_NAME_LENGTH,
    MCPClient,
    MCPTool,
    MCPSessionExpiredError,
    StdioMCPTransport,
    StreamableHTTPMCPTransport,
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


def test_configured_mcp_registration_returns_clients_without_runtime_container():
    client = object()

    class SessionPool:
        async def acquire(self, session_id, configs):
            assert session_id == "session-1"
            assert configs == [{"name": "docs"}]
            return [
                SimpleNamespace(
                    client=client,
                    prefix="mcp_docs",
                    tools=[],
                )
            ]

    class Registry:
        def register(self, **kwargs):
            raise AssertionError("empty discovered tool list should not register")

    async def run():
        return await register_configured_mcp_tools(
            {"mcp_servers": [{"name": "docs"}]},
            session_id="session-1",
            agent_id="smith",
            tool_registry=Registry(),
            session_pool=SessionPool(),
        )

    result = asyncio.run(run())
    assert result.clients == (client,)
    assert result.registered_tools == 0


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
    """Malformed tool metadata must not cross the MCP discovery boundary."""
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
                {"name": "bad-description", "description": 123, "inputSchema": {}},
                {
                    "name": "bad-schema",
                    "description": "schema is not an object",
                    "inputSchema": "not-an-object",
                },
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
                await register_mcp_tools_with_prefix(registry, client, prefix="mcp")
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
        return await register_mcp_tools_with_prefix(registry, FakeClient(), prefix="mcp")

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


@pytest.mark.parametrize("payload", [None, []])
def test_streamable_http_transport_rejects_non_object_jsonrpc_response(
    payload: object,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(204)
        if payload is None:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=b"null",
            )
        return httpx.Response(200, json=payload)

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        ) as http_client:
            transport = StreamableHTTPMCPTransport(
                "https://mcp.example.test/mcp",
                http_client=http_client,
            )
            try:
                await transport.send_request("tools/list", {})
            finally:
                await transport.close()

    with pytest.raises(RuntimeError, match="response must be a JSON object"):
        asyncio.run(run())


def test_streamable_http_transport_rejects_non_object_mcp_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(204)
        request_id = json.loads(request.content.decode())["id"]
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": request_id, "result": None},
        )

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        ) as http_client:
            transport = StreamableHTTPMCPTransport(
                "https://mcp.example.test/mcp",
                http_client=http_client,
            )
            try:
                await transport.send_request("tools/list", {})
            finally:
                await transport.close()

    with pytest.raises(RuntimeError, match="result must be a JSON object"):
        asyncio.run(run())


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


BIG_SERVER = r'''
import json
import sys


def send(payload):
    print(json.dumps(payload), flush=True)


for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")

    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}},
        })
        continue

    if method == "tools/call":
        params = message.get("params", {})
        size = int(params["arguments"]["size"])
        if params.get("name") == "flood":
            # Deliberately unterminated: the reader trips its limit while the
            # rest of this write is still in flight, which is the case that
            # destroys the newline framing.
            sys.stdout.write("F" * size)
            sys.stdout.flush()
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": [{"type": "text", "text": "x" * size}]},
        })
'''


async def _big_payload(tmp: Path, size: int) -> str:
    server = tmp / "big_server.py"
    server.write_text(BIG_SERVER, encoding="utf-8")
    client = MCPClient([sys.executable, str(server)])
    await client.connect()
    try:
        return await client.call_tool("big", {"size": size})
    finally:
        await client.close()


def test_stdio_transport_reads_response_above_default_stream_limit():
    """asyncio gives a subprocess StreamReader a 64 KiB limit by default, so
    readline() used to raise ValueError on any larger tool result -- and the
    unread bytes then blocked the server until close() killed it."""
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            return len(await _big_payload(Path(tmp), 200_000))

    assert asyncio.run(run()) == 200_000


def test_stdio_transport_retires_itself_after_an_oversized_response():
    """readline() clears its buffer on overrun while the rest of the oversized
    message is still arriving, so the newline framing is unrecoverable.  A size
    of exactly MAX + 1 lands in asyncio's other branch and looks clean, which
    is why this uses a payload big enough to leave bytes in the pipe: the next
    unrelated call used to read a fragment of them and raise either a bogus
    size error or a JSONDecodeError.  Once the framing is gone the transport
    has to refuse work rather than answer from someone else's bytes."""
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            server = Path(tmp) / "big_server.py"
            server.write_text(BIG_SERVER, encoding="utf-8")
            client = MCPClient([sys.executable, "-u", str(server)])
            await client.connect()
            outcomes: list[str] = []
            try:
                calls = (("flood", 3 * 1024 * 1024), ("small", 10), ("small", 10))
                for name, size in calls:
                    try:
                        await client.call_tool(name, {"size": size})
                        outcomes.append("ok")
                    except Exception as exc:  # noqa: BLE001 - classifying is the point
                        outcomes.append(f"{type(exc).__name__}:{exc}")
            finally:
                await client.close()
            return outcomes

    first, *rest = asyncio.run(run())

    assert first.startswith("RuntimeError:") and "exceeds maximum size" in first, first
    # Every later call fails the same, honest way -- never a JSONDecodeError
    # from a mis-framed read, and never a success built on stale bytes.
    assert all(outcome.startswith("RuntimeError:") for outcome in rest), rest
    assert all("retired" in outcome for outcome in rest), rest


def test_stdio_transport_merges_env_with_parent_environment():
    transport = StdioMCPTransport(["fake"], env={"ONLY_THIS": "value"})

    assert transport._env is not None
    assert transport._env["ONLY_THIS"] == "value"
    assert "PATH" in transport._env


def test_stdio_transport_does_not_inherit_parent_credentials(monkeypatch):
    """A configured-but-untrusted MCP server must not read the engine's secrets
    from its environment: only PATH, a small allowlist, and explicitly
    configured variables are forwarded."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret_value")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value-12345")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    transport = StdioMCPTransport(["fake"])

    assert transport._env["PATH"] == "/usr/bin:/bin"
    assert "GITHUB_TOKEN" not in transport._env
    assert "OPENAI_API_KEY" not in transport._env


def test_stdio_transport_forwards_explicitly_configured_variables():
    """An operator who explicitly grants a variable to an MCP server keeps it."""
    transport = StdioMCPTransport(
        ["fake"], env={"DATABASE_URL": "postgres://service:only-this@db"}
    )

    assert transport._env["DATABASE_URL"] == "postgres://service:only-this@db"
    assert "GITHUB_TOKEN" not in transport._env


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
        await register_mcp_tools_with_prefix(registry, FakeClient(), prefix="mcp")
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


def test_registered_mcp_tool_name_stays_within_provider_limit():
    """The prefix and the tool name were each capped at 64, but a provider
    applies its limit to the concatenation -- a long server alias plus a long
    tool name produced a 113-character name that OpenAI-compatible APIs
    reject with a 400.

    The two tool names below share every leading character and differ only in
    their last two, so plain truncation collapses them into one name and the
    duplicate registration is dropped with nothing but a log line.  Only the
    hash suffix keeps them apart -- which is what lets the distinctness
    assertion actually fail if that suffix is ever removed."""
    shared_head = "get_page_accessibility_snapshot_for_frame_" + "z" * 40

    class FakeClient:
        async def list_tools(self):
            return [MCPTool(f"{shared_head}_v1", "", {}), MCPTool(f"{shared_head}_v2", "", {})]

        async def call_tool(self, name, arguments):
            return name

    async def run():
        registry = ToolRegistry()
        prefix = _mcp_tool_prefix_from_config(
            {"name": "chrome-devtools-automation-server-production"}
        )
        count = await register_mcp_tools_with_prefix(registry, FakeClient(), prefix=prefix)
        return count, sorted(tool.name for tool in registry.list_tools())

    count, names = asyncio.run(run())

    assert all(len(name) <= MAX_TOOL_NAME_LENGTH for name in names), names
    assert (count, len(set(names))) == (2, 2), (
        f"tool names sharing a long head collapsed into one registration: {names}"
    )


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

    # The cap has to hold for the name that actually reaches the provider,
    # prefix included -- OpenAI-compatible APIs reject anything over 64.
    assert len(schema_name) <= MAX_TOOL_NAME_LENGTH
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


def test_stdio_transport_label_logs_executable_only():
    """The transport label feeds connect/registration log lines, so command
    arguments -- which routinely embed tokens -- must never appear in it."""
    transport = StdioMCPTransport(["npx", "-y", "mcp-server", "--token", "SECRET"])

    assert transport.label == "npx"
    assert "SECRET" not in transport.label


def test_http_transport_label_redacts_query_and_credentials():
    transport = StreamableHTTPMCPTransport(
        "https://user:pass@mcp.example.test/mcp?token=SECRET&scope=x#fragment"
    )

    assert transport.label == "https://mcp.example.test/mcp"
    assert "SECRET" not in transport.label
    assert "pass" not in transport.label


def test_mcp_server_log_summary_redacts_command_args_and_url_query():
    summary = _mcp_server_log_summary({
        "type": "stdio",
        "name": "docs",
        "command": ["npx", "-y", "mcp-server", "--token", "SECRET"],
        "url": "https://mcp.example.test/mcp?token=SECRET",
        "timeout": 30,
    })

    assert summary["command"] == "npx"
    assert summary["url"] == "https://mcp.example.test/mcp"
    assert "SECRET" not in repr(summary)


def test_stdio_transport_raises_when_response_stream_exceeds_budget(monkeypatch):
    """A server that keeps sending notifications must not hold a request open
    forever: the per-line timeout resets on every read, so a whole-request byte
    budget is the only bound on the wait."""
    import engine.mcp.client as mcp_client

    notification = b'{"jsonrpc":"2.0","method":"notifications/message","params":{"x":1}}\n'
    monkeypatch.setattr(mcp_client, "MAX_MCP_SSE_STREAM_BYTES", len(notification) * 3 + 1)

    class FakeStdin:
        def __init__(self) -> None:
            self.data = b""

        def write(self, data: bytes) -> None:
            self.data += data

        async def drain(self) -> None:
            return None

    class FakeStdout:
        def __init__(self) -> None:
            self.calls = 0

        async def readline(self) -> bytes:
            self.calls += 1
            return notification

    async def run():
        transport = StdioMCPTransport(["fake"])
        transport._process = SimpleNamespace(stdin=FakeStdin(), stdout=FakeStdout())
        try:
            await transport.send_request("tools/list", {})
        except RuntimeError as exc:
            return str(exc), transport._framing_broken, transport._process.stdout.calls
        raise AssertionError("whole-request byte budget was not enforced")

    message, framing_broken, reads = asyncio.run(run())

    assert "exceeds maximum total size" in message
    assert framing_broken is True
    assert reads == 4


def test_streamable_http_transport_clears_session_on_expiry():
    """A 404 for an established session must drop the stale MCP-Session-Id so
    the next request can re-initialize instead of replaying it into 404s."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(204)
        payload = json.loads(request.content.decode())
        method = payload.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"MCP-Session-Id": "session-1"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"protocolVersion": "2025-11-25"},
                },
            )
        if method == "tools/list":
            if "mcp-session-id" in request.headers:
                return httpx.Response(
                    404, headers={"MCP-Session-Id": "session-1"}, text="expired"
                )
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": []}},
            )
        raise AssertionError(method)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            transport = StreamableHTTPMCPTransport(
                "https://mcp.example.test/mcp", http_client=http_client,
            )
            transport._session_id = "session-1"
            try:
                await transport.send_request("tools/list", {})
            except MCPSessionExpiredError as exc:
                message = str(exc)
            else:
                raise AssertionError("session expiry did not raise typed error")
            # The stale id is cleared, so a follow-up request can succeed.
            result = await transport.send_request("tools/list", {})
            return message, transport._session_id, result

    message, session_id, result = asyncio.run(run())

    assert "session expired" in message
    assert session_id is None
    assert result == {"tools": []}


def test_mcp_openai_schemas_match_registered_names_for_prefix():
    """Schema advertisement must use the same per-prefix, de-duplicated names
    as registration so advertised names always equal registered names."""
    class FakeClient:
        async def list_tools(self):
            return [MCPTool("search", "", {})]

        async def call_tool(self, name, arguments):
            return name

    async def run():
        registry = ToolRegistry()
        prefix = "mcp_github"
        count = await register_mcp_tools_with_prefix(registry, FakeClient(), prefix=prefix)
        schemas = MCPClient([sys.executable, "-V"]).to_openai_schemas(
            [MCPTool("search", "", {})], prefix=prefix,
        )
        return count, [tool.name for tool in registry.list_tools()], [
            schema["function"]["name"] for schema in schemas
        ]

    count, registered, advertised = asyncio.run(run())

    assert count == 1
    assert advertised == ["mcp_github_search"]
    assert advertised == registered


def test_registered_mcp_tool_evicts_dead_connection_before_re_raise() -> None:
    """A fatal connection error (expired HTTP session) must invoke the pool's
    eviction hook so the next acquire reconnects; a plain tool-level failure
    must not evict a healthy connection."""

    class FailingClient:
        def __init__(self) -> None:
            self.tool = MCPTool("search", "", {})

        async def list_tools(self):
            return [self.tool]

        async def call_tool(self, name, arguments):
            mode = arguments.get("mode")
            if mode == "boom":
                raise MCPSessionExpiredError("MCP HTTP session expired")
            if mode == "soft-fail":
                raise RuntimeError("MCP tool returned an error result")
            return "ok"

    async def run():
        registry = ToolRegistry()
        evicted: list[str] = []

        async def on_failure() -> None:
            evicted.append("evicted")

        client = FailingClient()
        await register_mcp_tools_with_prefix(
            registry,
            client,
            prefix="mcp_test",
            on_connection_failure=on_failure,
        )
        expired = await registry.execute(
            ToolCall(id="1", name="mcp_test_search", arguments={"mode": "boom"})
        )
        soft = await registry.execute(
            ToolCall(id="2", name="mcp_test_search", arguments={"mode": "soft-fail"})
        )
        ok = await registry.execute(
            ToolCall(id="3", name="mcp_test_search", arguments={"mode": "ok"})
        )
        return evicted, expired, soft, ok

    evicted, expired, soft, ok = asyncio.run(run())

    assert evicted == ["evicted"]
    assert expired.is_error and "session expired" in expired.content
    assert soft.is_error and "returned an error result" in soft.content
    assert ok.content == "ok"


def test_is_fatal_connection_error_classifies_transport_deaths() -> None:
    from engine.mcp.client import is_fatal_connection_error

    assert is_fatal_connection_error(MCPSessionExpiredError("expired"))
    assert is_fatal_connection_error(RuntimeError("MCP server closed stdout unexpectedly"))
    assert is_fatal_connection_error(RuntimeError("MCP stdio transport not connected"))
    assert not is_fatal_connection_error(RuntimeError("MCP tool returned an error result"))
    assert not is_fatal_connection_error(TimeoutError("slow"))


def test_mcp_openai_schemas_deduplicate_colliding_names():
    schemas = MCPClient([sys.executable, "-V"]).to_openai_schemas([
        MCPTool("search-docs", "", {}),
        MCPTool("search_docs", "", {}),
    ])
    names = [schema["function"]["name"] for schema in schemas]

    assert names[0] == "mcp_search_docs"
    assert len(names) == 2
    assert len(set(names)) == 2


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
