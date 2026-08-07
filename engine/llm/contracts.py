"""Provider-neutral contracts owned by the :mod:`engine.llm` module.

Execution code intentionally talks only in terms of these values.  Provider
adapters are responsible for translating their wire formats into them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

# Conservative fallback when a model/route does not declare its real window.
DEFAULT_CONTEXT_WINDOW = 128_000
DEFAULT_MAX_OUTPUT_TOKENS = 4_096


class LLMError(RuntimeError):
    """Base error raised by the normalized LLM module."""


class LLMResponseError(LLMError):
    """Raised when a provider returns a payload outside the internal contract."""


class LLMContextLengthError(LLMResponseError):
    """Typed, sanitized rejection for an over-capacity model request."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        provider_code: str | None = None,
    ) -> None:
        self.http_status = http_status
        self.provider_code = provider_code
        super().__init__(message)


class UnsupportedProviderError(LLMError):
    """Raised when configuration names an adapter that is not registered."""


@dataclass(frozen=True)
class LLMTimeouts:
    """Phase-specific timeouts for one selected LLM execution route."""

    connect: float = 10.0
    read: float = 90.0
    stream_read: float = 120.0
    write: float = 30.0
    pool: float = 10.0

    def request_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect,
            read=self.read,
            write=self.write,
            pool=self.pool,
        )

    def stream_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect,
            read=self.stream_read,
            write=self.write,
            pool=self.pool,
        )


@dataclass(frozen=True)
class LLMRequest:
    """One provider-independent model request.

    ``messages`` and ``tools`` use the engine's existing conversation and tool
    representations.  Adapters translate them at their private seam.
    """

    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    prefix_cache_key: str | None = None


@dataclass(frozen=True)
class LLMProviderConfig:
    """Resolved connection configuration passed to an adapter factory."""

    provider: str
    api_key: str = field(repr=False)
    base_url: str
    model: str
    stream: bool = True
    timeouts: LLMTimeouts = field(default_factory=LLMTimeouts)
    max_output_tokens: int | None = None
    context_window: int | None = None


@dataclass(frozen=True)
class ModelLimits:
    """Normalized capacity facts used to fit provider requests."""

    context_window: int
    context_window_declared: bool
    max_output_tokens: int
    max_output_tokens_declared: bool


@dataclass(frozen=True)
class ProviderCapabilities:
    """Features an adapter can faithfully normalize for the execution layer."""

    streaming: bool = True
    tool_calls: bool = True
    prefix_cache_key: bool = False


@dataclass
class ToolCallData:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatResponse:
    """The complete normalized result of one model turn."""

    text: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCallData] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    raw_finish_reason: str | None = None
    # The model that actually served the call, as reported by the provider;
    # empty when the provider omits it (callers fall back to the config model).
    model: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)
