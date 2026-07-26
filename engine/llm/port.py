"""The small Interface consumed by execution code and tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from .contracts import ChatResponse, ModelLimits, ProviderCapabilities
from .events import ProviderEvent


@runtime_checkable
class LLMPort(Protocol):
    """Normalized model Interface, independent of any provider wire format."""

    stream: bool
    capabilities: ProviderCapabilities
    context_window: int
    context_window_declared: bool
    max_output_tokens: int
    max_output_tokens_declared: bool
    limits: ModelLimits
    model: str

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        prefix_cache_key: str | None = None,
    ) -> ChatResponse: ...

    def chat_events(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        prefix_cache_key: str | None = None,
    ) -> AsyncIterator[ProviderEvent]: ...

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
    ) -> AsyncIterator[str]: ...

    async def close(self) -> None: ...
