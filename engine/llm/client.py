"""Provider-neutral client facade over one concrete adapter.

New engine code should depend on :class:`engine.llm.port.LLMPort` and build
instances through :func:`engine.llm.factory.create_llm_client`.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from .adapters.base import ProviderAdapter
from .contracts import (
    ChatResponse,
    DEFAULT_MAX_OUTPUT_TOKENS,
    LLMRequest,
    LLMResponseError,
    ModelLimits,
    ToolCallData,
)
from .events import ProviderEvent, ProviderEventType
from .observability import (
    GenerationRecord,
    current_generation_scope,
    current_purpose,
    emit_generation,
)
from .usage import normalize_usage

_CONTENT_EVENT_TYPES = (
    ProviderEventType.OUTPUT_TEXT_DELTA,
    ProviderEventType.REASONING_DELTA,
    ProviderEventType.FUNCTION_CALL_ARGUMENTS_DELTA,
)


class ProviderClient:
    """Facade that exposes the normalized Interface over one Adapter."""

    def __init__(self, adapter: ProviderAdapter, *, stream: bool = True) -> None:
        self._adapter = adapter
        self.stream = stream
        self.provider = adapter.provider
        self.model = getattr(adapter, "model", "") or ""
        self.capabilities = adapter.capabilities
        self.context_window = adapter.context_window
        self.context_window_declared = adapter.context_window_declared
        configured_output = getattr(adapter, "max_output_tokens", None)
        self.max_output_tokens = (
            configured_output
            if isinstance(configured_output, int)
            and not isinstance(configured_output, bool)
            and configured_output > 0
            else DEFAULT_MAX_OUTPUT_TOKENS
        )
        self.max_output_tokens_declared = bool(
            getattr(adapter, "max_output_tokens_declared", False)
        )
        self.limits = ModelLimits(
            context_window=self.context_window,
            context_window_declared=self.context_window_declared,
            max_output_tokens=self.max_output_tokens,
            max_output_tokens_declared=self.max_output_tokens_declared,
        )

    @property
    def adapter(self) -> ProviderAdapter:
        """Expose the concrete Adapter only for module-level construction/tests."""
        return self._adapter

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        prefix_cache_key: str | None = None,
    ) -> ChatResponse:
        self._validate_requested_capabilities(tools, prefix_cache_key)
        started_at = time.monotonic()
        response: ChatResponse | None = None
        try:
            response = await self._adapter.complete(
                LLMRequest(
                    messages=messages,
                    tools=tools,
                    prefix_cache_key=prefix_cache_key,
                )
            )
            return response
        finally:
            await self._emit_generation(
                usage_raw=response.usage if response is not None else None,
                model=response.model if response is not None else "",
                ttft_ms=None,
                started_at=started_at,
                stream=False,
                ok=response is not None,
            )

    def chat_events(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        prefix_cache_key: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        self._validate_requested_capabilities(tools, prefix_cache_key)
        if not self.stream:
            # The non-streaming path funnels through ``chat()``, which already
            # emits the generation record; wrapping it again would double-count.
            return self._complete_as_events(messages, tools, prefix_cache_key)
        if not self.capabilities.streaming:
            raise LLMResponseError(f"Provider {self.provider} does not support streaming.")
        return self._observed_stream(LLMRequest(
            messages=messages,
            tools=tools,
            prefix_cache_key=prefix_cache_key,
        ))

    async def _observed_stream(self, request: LLMRequest) -> AsyncIterator[ProviderEvent]:
        """Relay the adapter stream while accounting one generation record."""
        # Attribution is captured eagerly: when a consumer abandons the stream
        # without closing it, cleanup runs in a GC-scheduled task whose context
        # no longer carries the caller's purpose/scope.
        purpose = current_purpose()
        scope = current_generation_scope()
        started_at = time.monotonic()
        ttft_ms: int | None = None
        usage_raw: dict[str, Any] | None = None
        served_model = ""
        ok = False
        try:
            async for event in self._adapter.stream_response(request):
                if ttft_ms is None and event.type in _CONTENT_EVENT_TYPES:
                    ttft_ms = max(0, int((time.monotonic() - started_at) * 1000))
                if event.type is ProviderEventType.USAGE:
                    usage = event.data.get("usage")
                    if isinstance(usage, dict):
                        usage_raw = usage
                elif event.type is ProviderEventType.RESPONSE_COMPLETED:
                    # The provider signaled completion; treat the generation as
                    # successful even if the consumer stops iterating now.
                    ok = True
                    model = event.data.get("model")
                    if isinstance(model, str) and model:
                        served_model = model
                yield event
        finally:
            await self._emit_generation(
                usage_raw=usage_raw,
                model=served_model,
                ttft_ms=ttft_ms,
                started_at=started_at,
                stream=True,
                ok=ok,
                purpose=purpose,
                scope=scope,
            )

    async def _emit_generation(
        self,
        *,
        usage_raw: object,
        model: str,
        ttft_ms: int | None,
        started_at: float,
        stream: bool,
        ok: bool,
        purpose: str | None = None,
        scope: tuple[str | None, str | None] | None = None,
    ) -> None:
        run_id, session_id = scope if scope is not None else current_generation_scope()
        await emit_generation(GenerationRecord(
            provider=self.provider,
            model=model or self.model,
            purpose=purpose if purpose is not None else current_purpose(),
            usage=normalize_usage(usage_raw),
            ttft_ms=ttft_ms,
            total_ms=max(0, int((time.monotonic() - started_at) * 1000)),
            stream=stream,
            ok=ok,
            run_id=run_id,
            session_id=session_id,
        ))

    def _validate_requested_capabilities(
        self,
        tools: list[dict[str, Any]] | None,
        prefix_cache_key: str | None,
    ) -> None:
        if tools and not self.capabilities.tool_calls:
            raise LLMResponseError(f"Provider {self.provider} does not support tool calls.")
        if prefix_cache_key and not self.capabilities.prefix_cache_key:
            raise LLMResponseError(f"Provider {self.provider} does not support a prefix cache key.")

    async def _complete_as_events(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        prefix_cache_key: str | None,
    ) -> AsyncIterator[ProviderEvent]:
        """Normalize a non-streaming completion into the same event contract."""
        # Await the completion before announcing RESPONSE_CREATED so a failing
        # model call never leaks a "response started" event to the consumer.
        response = await self.chat(
            messages,
            tools,
            prefix_cache_key=prefix_cache_key,
        )
        yield ProviderEvent(ProviderEventType.RESPONSE_CREATED, {"provider": self.provider})
        if response.reasoning:
            yield ProviderEvent(ProviderEventType.REASONING_DELTA, {"delta": response.reasoning})
        if response.text:
            yield ProviderEvent(ProviderEventType.OUTPUT_TEXT_DELTA, {"delta": response.text})
        for index, tool_call in enumerate(response.tool_calls):
            yield ProviderEvent(
                ProviderEventType.FUNCTION_CALL_ARGUMENTS_DELTA,
                {
                    "index": index,
                    "id": tool_call.id,
                    "name": tool_call.name,
                    "arguments_delta": json.dumps(tool_call.arguments, separators=(",", ":")),
                },
            )
        if response.usage is not None:
            yield ProviderEvent(ProviderEventType.USAGE, {"usage": response.usage})
        yield ProviderEvent(
            ProviderEventType.RESPONSE_COMPLETED,
            {
                "finish_reason": response.finish_reason,
                "raw_finish_reason": response.raw_finish_reason,
            },
        )

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
    ) -> AsyncIterator[str]:
        """Legacy text-only stream — yields only text deltas, no tool calls."""
        async for event in self.chat_events(messages):
            if event.type != ProviderEventType.OUTPUT_TEXT_DELTA:
                continue
            text = event.data.get("delta")
            if isinstance(text, str) and text:
                yield text

    async def close(self) -> None:
        await self._adapter.close()


__all__ = (
    "ChatResponse",
    "ProviderClient",
    "ToolCallData",
)
