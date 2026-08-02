"""Minimal MCP client for tool discovery and execution.

Implements the Model Context Protocol (MCP) over JSON-RPC 2.0, supporting
stdio and Streamable HTTP transports.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = {
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
}
CLIENT_INFO = {"name": "agent-smith", "version": "0.2.0"}
MAX_TOOL_NAME_LENGTH = 64
MAX_MCP_RESPONSE_BYTES = 1024 * 1024
# Whole-stream backstop for SSE and stdio: the per-message cap resets on
# every event/line, so only a server trickling data indefinitely should hit
# this ceiling.  Loose on purpose: legitimate long calls are unaffected.
MAX_MCP_SSE_STREAM_BYTES = 64 * MAX_MCP_RESPONSE_BYTES
MAX_MCP_TOOL_LIST_PAGES = 100
_FRAMING_BROKEN_MESSAGE = (
    "MCP stdio transport retired: an earlier response exceeded the maximum "
    "size and left the stream desynchronized"
)
# Messages that mark a stdio connection as unusable for any later request.
# A dead subprocess surfaces as EOF, and a retired transport refuses all
# future requests.  Both mean the session must reconnect, not retry.
_STDIO_DEAD_CONNECTION_MESSAGES = frozenset({
    "MCP server closed stdout unexpectedly",
    "MCP stdio transport not connected",
    _FRAMING_BROKEN_MESSAGE,
})


def _require_json_object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return value


def _parse_json_object(payload: str, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise RuntimeError(f"{label} contains invalid JSON") from exc
    return _require_json_object(value, label=label)


@dataclass
class MCPTool:
    """A tool discovered from an MCP server."""

    name: str
    description: str
    input_schema: dict


class MCPToolError(RuntimeError):
    """Raised when an MCP tool reports a failed tool result."""


class MCPConnectError(RuntimeError):
    """Raised when every configured MCP server fails to connect.

    Catchable by a session pool: nothing is cached for a fingerprint whose
    connect failed, so the caller can surface the failure instead of losing
    all MCP tools for the session.
    """


class MCPSessionExpiredError(RuntimeError):
    """Raised when a Streamable HTTP MCP session has expired server-side.

    The transport clears its stale session id before raising so a later
    request can re-initialize; callers may evict the connection from a pool.
    """


def is_fatal_connection_error(exc: BaseException) -> bool:
    """Whether an MCP tool-call exception means the connection is unusable.

    A session that expires server-side (HTTP 404), a stdio subprocess that
    closed stdout, and a framing-retired transport can never serve another
    request: the caller must drop and reconnect rather than retry against a
    dead connection.  Tool-level failures (``MCPToolError``), timeouts, and
    other transient errors are explicitly NOT fatal — they do not evict.
    """
    if isinstance(exc, MCPSessionExpiredError):
        return True
    if isinstance(exc, RuntimeError):
        message = str(exc)
        if message in _STDIO_DEAD_CONNECTION_MESSAGES:
            return True
    return False


class MCPTransport(Protocol):
    label: str

    async def connect(self) -> None:
        ...

    async def send_request(self, method: str, params: dict) -> dict:
        ...

    async def send_notification(self, method: str, params: dict) -> None:
        ...

    async def close(self) -> None:
        ...


def _redact_command(command: list[str]) -> str:
    """Log only the executable name, never arguments that may embed tokens."""
    if not command:
        return "<no-command>"
    return command[0]


def _redact_url(url: str) -> str:
    """Strip query/fragment and embedded credentials from a URL for logging.

    URL query parameters and basic-auth userinfo routinely carry tokens; the
    transport label and log summaries must never reproduce them.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    netloc = parsed.netloc
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        try:
            netloc = f"{host}:{parsed.port}" if parsed.port else host
        except ValueError:
            # Malformed port in the netloc: keep the host only, never the
            # credentials, and never fail the redaction path on user input.
            netloc = host
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


# Locale/terminal keys safe to forward to an MCP stdio server.  Everything else
# is deliberately NOT inherited from the parent process: a configured-but-
# untrusted server (an arbitrary npx package) must not be able to read the
# engine's API keys, DB credentials, or other secrets from its environment.
_MCP_SAFE_ENV_KEYS = ("LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "NO_COLOR")


def _mcp_subprocess_environment(env: dict[str, str] | None) -> dict[str, str]:
    """Build the environment an MCP stdio subprocess receives.

    Mirrors the credential-free ``_safe_environment`` used for model-requested
    shells: PATH and a small locale/terminal allowlist come from the parent,
    plus any variables the user explicitly configured for this server.  No
    other parent variables are forwarded, so a server can only see secrets the
    operator deliberately granted it.
    """
    environment: dict[str, str] = {
        "PATH": os.environ.get("PATH") or os.defpath,
    }
    for key in _MCP_SAFE_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            environment[key] = value
    if env is not None:
        environment.update(env)
    return environment


class StdioMCPTransport:
    """MCP transport backed by a local subprocess' stdin/stdout."""

    def __init__(
        self,
        command: list[str],
        env: dict[str, str] | None = None,
        *,
        close_timeout: float = 5.0,
    ) -> None:
        self._command = command
        self._env = _mcp_subprocess_environment(env)
        self._close_timeout = close_timeout
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_drain_task: asyncio.Task[None] | None = None
        self._request_id = 0
        # stdio is a single shared pipe: concurrent send_request calls would
        # interleave reads and one waiter would consume (and drop) another
        # waiter's response, so request/response exchanges are serialized.
        self._request_lock = asyncio.Lock()
        # Set once a response overruns the read buffer.  readline() clears its
        # buffer on overrun while the rest of that message is still arriving,
        # so the newline framing is gone for good and no later request on this
        # pipe can trust what it reads.
        self._framing_broken = False
        self.label = _redact_command(command)

    async def connect(self) -> None:
        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
            # asyncio caps a subprocess StreamReader at 64 KiB by default,
            # which silently sits 16x below the size this transport already
            # declares it accepts.  A tools/list from a server with many
            # tools clears 64 KiB easily, so the default made those servers
            # unusable rather than merely slow.
            limit=MAX_MCP_RESPONSE_BYTES,
        )
        # An MCP server's stderr is diagnostic-only, but it is still a pipe.
        # If nobody consumes it, a noisy or malicious server can fill the OS
        # pipe buffer and deadlock before it writes its JSON-RPC response.
        assert self._process.stderr is not None
        self._stderr_drain_task = asyncio.create_task(
            self._drain_stderr(self._process.stderr)
        )

    @staticmethod
    async def _drain_stderr(stream: asyncio.StreamReader) -> None:
        while await stream.read(4096):
            pass

    @staticmethod
    async def _drain_stdout_after_kill(process: asyncio.subprocess.Process) -> None:
        """Let the subprocess transport finish once the process is dead.

        A stdout reader that nobody consumes pauses at its high-water mark, and
        a paused reader never observes EOF -- so the transport keeps its pipes
        open and ``process.wait()`` blocks forever, even after SIGKILL.  Only
        safe once the process is killed: before that, draining to EOF would
        wait on a healthy server that has no reason to exit.
        """
        stdout = getattr(process, "stdout", None)
        if stdout is None:
            return
        try:
            while await asyncio.wait_for(stdout.read(65536), timeout=5):
                pass
        except Exception:
            log.debug("failed to drain MCP server stdout during shutdown", exc_info=True)

    async def send_request(self, method: str, params: dict) -> dict:
        if self._process is None or self._process.stdin is None or self._process.stdout is None:
            raise RuntimeError("MCP stdio transport not connected")

        async with self._request_lock:
            if self._framing_broken:
                raise RuntimeError(_FRAMING_BROKEN_MESSAGE)
            self._request_id += 1
            request_id = self._request_id
            msg = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
            self._process.stdin.write((json.dumps(msg) + "\n").encode())
            await self._process.stdin.drain()

            # The per-line timeout below resets on every read, so a server
            # sending periodic notifications can otherwise hold this request
            # open forever.  A whole-request byte budget bounds the wait the
            # same way MAX_MCP_SSE_STREAM_BYTES bounds an SSE stream.
            total_bytes = 0
            while True:
                try:
                    line = await asyncio.wait_for(self._process.stdout.readline(), timeout=30)
                except ValueError as exc:
                    # Over the buffer limit readline() raises a bare
                    # ValueError.  Unlike the HTTP transport -- which is
                    # stateless per request and so cannot poison the next one
                    # -- the rest of the oversized message keeps arriving on
                    # this shared pipe, and a later request would read a
                    # fragment of it and either mis-pair a response or fail to
                    # parse one.  Retire the transport instead of leaving the
                    # session with a connection that silently lies.
                    self._framing_broken = True
                    raise RuntimeError("MCP stdio response exceeds maximum size") from exc
                if not line:
                    raise RuntimeError("MCP server closed stdout unexpectedly")
                total_bytes += len(line)
                if total_bytes > MAX_MCP_SSE_STREAM_BYTES:
                    # We abandon the read mid-stream, so any bytes the server
                    # is still writing would desynchronize the next request.
                    self._framing_broken = True
                    raise RuntimeError("MCP stdio response stream exceeds maximum total size")
                resp = _parse_json_object(
                    line.decode(),
                    label="MCP stdio response",
                )
                if resp.get("id") != request_id:
                    log.debug("Ignoring MCP message while waiting for id %s: %s", request_id, resp)
                    continue
                break

        if "error" in resp:
            raise RuntimeError(f"MCP error: {resp['error']}")
        return _require_json_object(
            resp.get("result", {}),
            label="MCP result",
        )

    async def send_notification(self, method: str, params: dict) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("MCP stdio transport not connected")

        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        self._process.stdin.write((json.dumps(msg) + "\n").encode())
        await self._process.stdin.drain()

    async def close(self) -> None:
        if self._process is None:
            return
        process = self._process
        if process.stdin:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=self._close_timeout)
        except asyncio.TimeoutError:
            process.kill()
            await self._drain_stdout_after_kill(process)
            await process.wait()
            log.warning("MCP server killed after timeout")
        except asyncio.CancelledError:
            # Dropping the transport reference during cancellation must not
            # orphan a configured MCP server.
            if process.returncode is None:
                process.kill()
                try:
                    await asyncio.shield(self._drain_stdout_after_kill(process))
                    await asyncio.shield(process.wait())
                except Exception:
                    log.warning("failed to reap cancelled MCP server", exc_info=True)
            raise
        finally:
            self._process = None
            drain_task = self._stderr_drain_task
            self._stderr_drain_task = None
            if drain_task is not None:
                if not drain_task.done():
                    drain_task.cancel()
                await asyncio.gather(drain_task, return_exceptions=True)


class StreamableHTTPMCPTransport:
    """MCP transport backed by a Streamable HTTP endpoint."""

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        http_client: Any | None = None,
    ) -> None:
        self._url = url
        self._headers = headers or {}
        self._timeout = timeout
        self._request_id = 0
        self._client: Any | None = http_client
        self._owns_client = http_client is None
        self._session_id: str | None = None
        self._protocol_version = PROTOCOL_VERSION
        self.label = _redact_url(url)

    async def connect(self) -> None:
        if self._client is not None:
            return
        try:
            import httpx
        except Exception as exc:
            raise RuntimeError("httpx is required for Streamable HTTP MCP transport") from exc
        self._client = httpx.AsyncClient(timeout=self._timeout)

    async def send_request(self, method: str, params: dict) -> dict:
        if self._client is None:
            raise RuntimeError("MCP HTTP transport not connected")

        self._request_id += 1
        request_id = self._request_id
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        async with self._client.stream(
            "POST",
            self._url,
            json=message,
            headers=self._request_headers(
                accept="application/json, text/event-stream",
                include_protocol=method != "initialize",
            ),
        ) as response:
            if response.status_code == 404 and self._session_id:
                # The server discarded our session.  Drop the stale id so the
                # next request can re-initialize instead of replaying it into a
                # 404 forever, and raise a typed error the pool can evict on.
                self._session_id = None
                raise MCPSessionExpiredError("MCP HTTP session expired")
            response.raise_for_status()
            self._capture_session(response.headers)

            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
            if content_type == "text/event-stream":
                resp = await _response_from_sse_stream(response, request_id)
            else:
                resp = _parse_json_object(
                    (await _read_bounded_response(response)).decode(),
                    label="MCP HTTP response",
                )

        if resp.get("id") != request_id:
            raise RuntimeError(f"MCP HTTP response id mismatch: {resp.get('id')!r} != {request_id!r}")
        if "error" in resp:
            raise RuntimeError(f"MCP error: {resp['error']}")

        result = _require_json_object(
            resp.get("result", {}),
            label="MCP result",
        )
        if method == "initialize":
            protocol_version = result.get("protocolVersion")
            if isinstance(protocol_version, str) and protocol_version in SUPPORTED_PROTOCOL_VERSIONS:
                self._protocol_version = protocol_version
        return result

    async def send_notification(self, method: str, params: dict) -> None:
        if self._client is None:
            raise RuntimeError("MCP HTTP transport not connected")

        message = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        async with self._client.stream(
            "POST",
            self._url,
            json=message,
            headers=self._request_headers(
                accept="application/json, text/event-stream",
                include_protocol=True,
            ),
        ) as response:
            if response.status_code not in (200, 202, 204):
                response.raise_for_status()
            self._capture_session(response.headers)
            await _read_bounded_response(response)

    async def close(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.delete(
                self._url,
                headers=self._request_headers(accept="application/json", include_protocol=True),
            )
        except Exception:
            log.debug("MCP HTTP session shutdown request failed", exc_info=True)
        if self._owns_client:
            await self._client.aclose()
        self._client = None

    def _request_headers(self, *, accept: str, include_protocol: bool) -> dict[str, str]:
        headers = {
            "Accept": accept,
            "Content-Type": "application/json",
            **self._headers,
        }
        if include_protocol:
            headers["MCP-Protocol-Version"] = self._protocol_version
        if self._session_id:
            headers["MCP-Session-Id"] = self._session_id
        return headers

    def _capture_session(self, headers: Any) -> None:
        session_id = headers.get("MCP-Session-Id")
        if isinstance(session_id, str) and session_id:
            self._session_id = session_id


class MCPClient:
    """MCP client that discovers and invokes remote tools via a transport."""

    def __init__(
        self,
        command: list[str] | None = None,
        env: dict[str, str] | None = None,
        *,
        transport: MCPTransport | None = None,
    ) -> None:
        if transport is None:
            if command is None:
                raise ValueError("MCPClient requires a command or transport")
            transport = StdioMCPTransport(command, env=env)
        self._transport = transport
        self.protocol_version = PROTOCOL_VERSION

    async def connect(self) -> None:
        """Connect to the MCP server and perform the initialize handshake."""
        await self._transport.connect()
        try:
            result = await self._send("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            })
            protocol_version = result.get("protocolVersion")
            if not isinstance(protocol_version, str) or protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
                raise RuntimeError(f"Unsupported MCP protocol version: {protocol_version!r}")
            self.protocol_version = protocol_version
            await self._notify("notifications/initialized", {})
            log.info("MCP client connected to: %s", self._transport.label)
        except BaseException:
            try:
                await self.close()
            except BaseException:
                log.warning("failed to close MCP transport after connect failure", exc_info=True)
            raise

    async def list_tools(self) -> list[MCPTool]:
        """Discover available tools from the MCP server."""
        tools: list[MCPTool] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _page in range(MAX_MCP_TOOL_LIST_PAGES):
            params = {"cursor": cursor} if cursor else {}
            result = await self._send("tools/list", params)
            for t in result.get("tools", []):
                if not isinstance(t, dict):
                    continue
                name = t.get("name")
                if not isinstance(name, str) or not name:
                    continue
                description = t.get("description", "")
                input_schema = t.get("inputSchema", {})
                if not isinstance(description, str) or not isinstance(input_schema, dict):
                    log.warning("Skipping MCP tool with invalid metadata: %s", name)
                    continue
                tools.append(MCPTool(
                    name=name,
                    description=description,
                    input_schema=input_schema,
                ))
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return tools
            if next_cursor in seen_cursors:
                raise RuntimeError("MCP tools/list returned a repeated cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise RuntimeError(
            f"MCP tools/list exceeded maximum page limit ({MAX_MCP_TOOL_LIST_PAGES})"
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Call a tool on the MCP server and return the text result."""
        result = await self._send("tools/call", {"name": name, "arguments": arguments})
        content = _content_to_text(result.get("content", []))
        if result.get("isError") is True:
            raise MCPToolError(content or f"MCP tool failed: {name}")
        return content

    def to_openai_schemas(
        self,
        tools: list[MCPTool],
        *,
        prefix: str = "mcp",
    ) -> list[dict]:
        """Convert MCP tools to OpenAI function calling format.

        Names follow the same per-prefix, de-duplicated path as
        ``register_mcp_tools_with_prefix`` so advertised names always equal the
        names actually registered.  Pass the same prefix used for registration.
        """
        schemas: list[dict] = []
        taken: set[str] = set()
        for tool in tools:
            registered_name = _mcp_registered_name(prefix, tool.name, taken)
            if registered_name is None:
                continue
            taken.add(registered_name)
            schemas.append({
                "type": "function",
                "function": {
                    "name": registered_name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            })
        return schemas

    async def _send(self, method: str, params: dict) -> dict:
        return await self._transport.send_request(method, params)

    async def _notify(self, method: str, params: dict) -> None:
        await self._transport.send_notification(method, params)

    async def close(self) -> None:
        """Shut down the MCP transport."""
        await self._transport.close()


async def register_mcp_tools(registry: Any, client: MCPClient) -> int:
    """Discover MCP tools and register them into a ToolRegistry.

    Each tool is registered with an ``mcp_`` prefix to avoid name
    collisions. Returns the number of tools registered.
    """
    return await register_mcp_tools_with_prefix(registry, client, prefix="mcp")


async def register_mcp_tools_with_prefix(
    registry: Any,
    client: MCPClient,
    *,
    prefix: str,
    tools: list[MCPTool] | None = None,
    on_connection_failure: Any | None = None,
) -> int:
    """Discover MCP tools and register them into a ToolRegistry.

    ``on_connection_failure`` is an optional async zero-argument callback
    invoked (before the error re-raises) when a tool call fails because the
    underlying connection is dead — an expired HTTP session or a closed stdio
    pipe.  A session pool passes an eviction hook here so the next acquire
    reconnects instead of reusing a dead server.
    """
    tools = tools if tools is not None else await client.list_tools()
    count = 0
    taken: set[str] = set()
    for tool in tools:
        # 闭包捕获 — 使用默认参数绑定当前迭代值
        async def _execute(*, _client: MCPClient = client, _name: str = tool.name, **kwargs: Any) -> str:
            try:
                return await _client.call_tool(_name, kwargs)
            except BaseException as exc:
                if on_connection_failure is not None and is_fatal_connection_error(exc):
                    try:
                        await on_connection_failure()
                    except Exception:
                        log.warning(
                            "MCP connection-failure handler raised (tool=%s)",
                            _name,
                            exc_info=True,
                        )
                raise

        # Cap the joined name, not the two halves: a provider applies its
        # 64-character limit to what it actually receives.
        registered_name = _mcp_registered_name(prefix, tool.name, taken)
        if registered_name is None:
            log.warning("Skipping MCP tool with empty/invalid name: %r", tool.name)
            continue
        try:
            registry.register(
                name=registered_name,
                description=tool.description,
                parameters=tool.input_schema,
                func=_execute,
                # A remote MCP tool's real side effect cannot be inferred
                # safely from server-supplied metadata.  Default to explicit
                # approval and serialize calls until a trusted integration
                # can declare a narrower contract.
                permission_level="execute",
                approval_policy="always",
                side_effect="external",
                concurrency="serial",
            )
            taken.add(registered_name)
            count += 1
        except ValueError as exc:
            log.warning("Skipping MCP tool %s: %s", tool.name, exc)
        except Exception:
            log.exception("Failed to register MCP tool: %s", tool.name)

    log.info("Registered %d MCP tools from %s", count, _client_label(client))
    return count


def _content_to_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            parts.append(str(part))
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            parts.append(part["text"])
        else:
            parts.append(json.dumps(part, ensure_ascii=False))
    return "\n".join(parts)


async def _response_from_sse_stream(response: Any, request_id: int) -> dict:
    async for payload in _iter_sse_data_stream(response):
        if not payload:
            continue
        message = _parse_json_object(payload, label="MCP SSE response")
        if message.get("id") == request_id:
            return message
        log.debug("Ignoring MCP SSE message while waiting for id %s: %s", request_id, message)
    raise RuntimeError(f"MCP SSE stream ended before response id {request_id}")


async def _iter_sse_data_stream(response: Any):
    data_lines: list[str] = []
    payload_size = 0
    stream_size = 0
    async for raw_line in response.aiter_lines():
        line = raw_line.rstrip("\r")
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines = []
                # Reset per event: this cap bounds a single message, and a long
                # call legitimately streams many small progress notifications
                # before its response arrives.
                payload_size = 0
            continue
        if line.startswith("data:"):
            payload = line[5:].lstrip()
            size = len(payload.encode("utf-8"))
            payload_size += size
            stream_size += size
            if payload_size > MAX_MCP_RESPONSE_BYTES:
                raise RuntimeError("MCP SSE response exceeds maximum size")
            # Per-event reset above removes any bound on the stream as a whole,
            # which would let a server trickle small events forever (httpx's
            # timeout is per-read and resets on every chunk).  Keep a deliberately
            # loose ceiling so legitimate long calls are unaffected.
            if stream_size > MAX_MCP_SSE_STREAM_BYTES:
                raise RuntimeError("MCP SSE stream exceeds maximum total size")
            data_lines.append(payload)
    if data_lines:
        yield "\n".join(data_lines)


async def _read_bounded_response(response: Any) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_MCP_RESPONSE_BYTES:
            raise RuntimeError("MCP HTTP response exceeds maximum size")
        chunks.append(chunk)
    return b"".join(chunks)


def _safe_tool_name_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", value.strip())
    normalized = "_".join(part for part in cleaned.split("_") if part)
    if len(normalized) <= MAX_TOOL_NAME_LENGTH:
        return normalized
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]
    head = normalized[: MAX_TOOL_NAME_LENGTH - len(digest) - 1].rstrip("_")
    return f"{head}_{digest}"


def _deduplicate_tool_name(registered_name: str, original: str, taken: set[str]) -> str:
    """Give a colliding registered name a distinct suffix.

    Cleaning is intentionally lossy — ``safe-tool`` is meant to become
    ``safe_tool`` — but two server-side names can fold onto one registered name
    (``search-docs`` and ``search_docs``), and the loser used to hit
    ``register()``'s duplicate-name error and vanish from the session behind a
    warning.  Suffix only the actual collision, so names that do not collide keep
    their exact spelling.
    """
    if registered_name not in taken:
        return registered_name
    digest = hashlib.sha1(original.encode("utf-8")).hexdigest()[:8]
    head = registered_name[: MAX_TOOL_NAME_LENGTH - len(digest) - 1].rstrip("_")
    return f"{head}_{digest}"


def _mcp_registered_name(prefix: str, tool_name: str, taken: set[str]) -> str | None:
    """Build the registered tool name shared by registration and schema
    advertisement.

    Two server-side names can fold onto one registered name; ``taken`` applies
    the same collision suffix the registry would, so an advertised schema name
    always equals the name actually registered.  Returns None when the tool
    name cleans down to nothing.
    """
    safe_prefix = _safe_tool_name_part(prefix) or "mcp"
    tool_part = _safe_tool_name_part(tool_name)
    if not tool_part:
        return None
    return _deduplicate_tool_name(
        _safe_tool_name_part(f"{safe_prefix}_{tool_part}"), tool_name, taken
    )


def _client_label(client: Any) -> str:
    transport = getattr(client, "_transport", None)
    label = getattr(transport, "label", None)
    return label if isinstance(label, str) and label else type(client).__name__
