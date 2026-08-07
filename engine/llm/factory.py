"""Provider registry and construction for the ``engine.llm`` module."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from .adapters.anthropic import AnthropicAdapter
from .adapters.base import ProviderAdapter
from .adapters.openai import OpenAIAdapter
from .client import ProviderClient
from .contracts import LLMProviderConfig, UnsupportedProviderError


AdapterBuilder = Callable[[LLMProviderConfig], ProviderAdapter]


class ProviderRegistry:
    """Registry at the private Adapter seam, with explicit supported names."""

    def __init__(self) -> None:
        self._builders: dict[str, AdapterBuilder] = {}
        self._aliases: dict[str, str] = {}

    def register(
        self,
        provider: str,
        builder: AdapterBuilder,
        *,
        aliases: tuple[str, ...] = (),
    ) -> None:
        canonical = self._clean_name(provider)
        if canonical in self._builders:
            raise ValueError(f"LLM provider is already registered: {canonical}")
        self._builders[canonical] = builder
        self._aliases[canonical] = canonical
        for alias in aliases:
            normalized_alias = self._clean_name(alias)
            if normalized_alias in self._aliases:
                raise ValueError(f"LLM provider alias is already registered: {normalized_alias}")
            self._aliases[normalized_alias] = canonical

    def normalize(self, provider: object) -> str:
        if provider is None or (isinstance(provider, str) and not provider.strip()):
            return "openai"
        if not isinstance(provider, str):
            raise UnsupportedProviderError("LLM provider must be a string.")
        name = self._clean_name(provider)
        canonical = self._aliases.get(name)
        if canonical is None:
            supported = ", ".join(self.supported_names())
            raise UnsupportedProviderError(
                f"Unsupported LLM provider {provider!r}; supported providers: {supported}."
            )
        return canonical

    def create(self, config: LLMProviderConfig) -> ProviderAdapter:
        canonical = self.normalize(config.provider)
        return self._builders[canonical](replace(config, provider=canonical))

    def supported_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._aliases))

    @staticmethod
    def _clean_name(value: str) -> str:
        normalized = value.strip().lower().replace("-", "_")
        if not normalized:
            raise ValueError("LLM provider name cannot be empty.")
        return normalized


DEFAULT_PROVIDER_REGISTRY = ProviderRegistry()
DEFAULT_PROVIDER_REGISTRY.register(
    "openai",
    OpenAIAdapter,
    aliases=("openai_compatible",),
)
DEFAULT_PROVIDER_REGISTRY.register("anthropic", AnthropicAdapter)


def normalize_provider_name(provider: object) -> str:
    return DEFAULT_PROVIDER_REGISTRY.normalize(provider)


def supported_provider_names() -> tuple[str, ...]:
    return DEFAULT_PROVIDER_REGISTRY.supported_names()


def create_llm_client(config: LLMProviderConfig) -> ProviderClient:
    """Create one normalized client from a validated resolved configuration."""
    return ProviderClient(
        DEFAULT_PROVIDER_REGISTRY.create(config),
        stream=config.stream,
    )
