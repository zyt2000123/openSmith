"""Internal Adapter Interface for provider-specific implementations."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from ..contracts import ChatResponse, LLMRequest, ProviderCapabilities
from ..events import ProviderEvent


@runtime_checkable
class ProviderAdapter(Protocol):
    """Satisfies the private Adapter seam behind :class:`engine.llm.LLMPort`."""

    provider: str
    capabilities: ProviderCapabilities
    context_window: int
    context_window_declared: bool
    max_output_tokens: int | None
    max_output_tokens_declared: bool

    async def complete(self, request: LLMRequest) -> ChatResponse: ...

    def stream_response(self, request: LLMRequest) -> AsyncIterator[ProviderEvent]: ...

    async def close(self) -> None: ...
