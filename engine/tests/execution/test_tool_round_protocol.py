"""Exercise tool follow-ups through the real HTTP adapter's protocol boundary."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from engine.execution.events import EventType
from engine.execution.react.react_loop import react_event_loop
from engine.llm.adapters.openai import OpenAIAdapter
from engine.llm.client import ProviderClient
from engine.llm.contracts import LLMProviderConfig
from engine.safety.approval import ApprovalBroker, use_approval_context
from engine.safety.tool_guard import GuardResult
from engine.tool.registry import ToolRegistry


def _protocol_error(messages: list[dict]) -> str | None:
    pending: set[str] = set()
    for message in messages:
        if message["role"] == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending:
                return "orphan tool result"
            pending.remove(call_id)
        else:
            if pending:
                return "non-tool message before all tool results"
            pending = {call["id"] for call in message.get("tool_calls", [])}
    return "missing tool results" if pending else None


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("time_first", [True, False])
@pytest.mark.parametrize("hook_mode", [None, "warn", "block", "approve", "deny"])
def test_clock_and_search_follow_up_preserves_tool_protocol(stream, time_first, hook_mode):
    """The screenshot's clock + search batch must survive a strict provider."""
    requests: list[dict] = []
    executions: list[str] = []
    approvals: list[str] = []
    tool_names = ["get_current_time", "web_search"]
    if not time_first:
        tool_names.reverse()
    reasoning = "Check the clock and search before answering."

    class Guard:
        def check(self, call):
            if call.name == "web_search":
                return GuardResult(
                    allowed=False, approval_required=True, reason="Approve search",
                )
            return GuardResult(allowed=True)

    class Hooks:
        async def run_pre_hooks(self, name, arguments):
            if hook_mode == "block" and name == "web_search":
                return False, "Search blocked by hook"
            return True, None

        async def run_post_hooks(self, name, arguments, result):
            return ["Search hook warning"] if hook_mode == "warn" and name == "web_search" else []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        error = _protocol_error(body["messages"])
        if error:
            return httpx.Response(400, json={"error": {"message": error}})
        if len(requests) == 1:
            message = {
                "role": "assistant", "content": "", "reasoning_content": reasoning,
                "tool_calls": [
                    {"id": f"call-{i}", "type": "function", "function": {
                        "name": name, "arguments": "{}",
                    }} for i, name in enumerate(tool_names)
                ],
            }
            finish_reason = "tool_calls"
        else:
            message = {"role": "assistant", "content": "Market summary."}
            finish_reason = "stop"
        if stream:
            delta = dict(message)
            if "tool_calls" in delta:
                delta["tool_calls"] = [
                    {"index": i, **call} for i, call in enumerate(delta["tool_calls"])
                ]
            chunk = {"choices": [{"delta": delta, "finish_reason": finish_reason}]}
            return httpx.Response(200, content=(
                f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
            ).encode(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{
            "message": message, "finish_reason": finish_reason,
        }]})

    async def run():
        adapter = OpenAIAdapter(LLMProviderConfig(
            provider="openai", api_key="test", base_url="https://provider.test", model="test",
        ))
        await adapter._http.aclose()
        adapter._http = httpx.AsyncClient(
            base_url="https://provider.test", transport=httpx.MockTransport(handle),
        )
        client = ProviderClient(adapter, stream=stream)
        registry = ToolRegistry()

        async def current_time():
            executions.append("get_current_time")
            return '{"local_time":"2026-10-18T12:47:06+08:00"}'

        async def search():
            executions.append("web_search")
            return "Search results for A shares."

        registry.register("get_current_time", "Read the clock", {}, current_time)
        registry.register("web_search", "Search the web", {}, search)
        try:
            broker = ApprovalBroker()
            text: list[str] = []
            with use_approval_context(broker, "protocol-test"):
                async for event in react_event_loop(
                    client, [
                        {"role": "system", "content": "Stable system prefix"},
                        {"role": "user", "content": "最新的 A 股行情"},
                    ], registry,
                    tool_guard=Guard() if hook_mode in {"approve", "deny"} else None,
                    hook_registry=Hooks() if hook_mode in {"warn", "block"} else None,
                ):
                    if event.type is EventType.TEXT_DELTA:
                        text.append(event.data["text"])
                    if event.type is EventType.TOOL_CALL_RESULT:
                        if event.data.get("approval_required"):
                            approvals.append(event.data["tool"])
                            assert broker.resolve(
                                "protocol-test", event.data["approval_id"], hook_mode == "approve",
                            )
            return "".join(text)
        finally:
            await client.close()

    assert asyncio.run(run()) == "Market summary."
    assert len(requests) == 2
    messages = requests[1]["messages"]
    assistant = next(message for message in messages if message.get("tool_calls"))
    assert assistant["reasoning_content"] == reasoning
    assert [message["role"] for message in messages[:5]] == [
        "system", "user", "assistant", "tool", "tool",
    ]
    assert messages[:2] == requests[0]["messages"]
    assert any(message["content"].startswith("[Current time]\n") for message in messages[5:])
    assert [message["tool_call_id"] for message in messages[3:5]] == ["call-0", "call-1"]
    if hook_mode in {"block", "deny"}:
        assert executions == ["get_current_time"]
        denial = "Search blocked by hook" if hook_mode == "block" else "User denied approval"
        assert any(message["content"] == f"[BLOCKED] {denial}" for message in messages[3:5])
    else:
        assert executions == tool_names
    if hook_mode == "warn":
        assert {"role": "system", "content": "Search hook warning"} in messages[5:]
    assert approvals == (["web_search"] if hook_mode in {"approve", "deny"} else [])
