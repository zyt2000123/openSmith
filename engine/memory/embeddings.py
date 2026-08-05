"""Optional OpenAI-compatible embedding provider configured by Smith's profile."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class OpenAICompatibleEmbeddingProvider:
    base_url: str
    api_key: str
    model: str

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                self.base_url.rstrip("/") + "/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "input": texts},
            )
            response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ValueError("embedding response did not contain data")
        vectors = [item.get("embedding") for item in data if isinstance(item, dict)]
        if len(vectors) != len(texts) or not all(
            isinstance(vector, list) and all(isinstance(value, (int, float)) for value in vector)
            for vector in vectors
        ):
            raise ValueError("embedding response shape was invalid")
        return [[float(value) for value in vector] for vector in vectors]


def embedding_provider_from_config(config: dict[str, Any]) -> OpenAICompatibleEmbeddingProvider | None:
    knowledge = config.get("knowledge") if isinstance(config, dict) else None
    raw = knowledge.get("embeddings") if isinstance(knowledge, dict) else None
    if not isinstance(raw, dict) or raw.get("enabled") is not True:
        return None
    base_url = raw.get("base_url")
    model = raw.get("model")
    api_key_env = raw.get("api_key_env")
    if not all(isinstance(value, str) and value.strip() for value in (base_url, model, api_key_env)):
        return None
    api_key = os.getenv(api_key_env)
    if not api_key:
        return None
    return OpenAICompatibleEmbeddingProvider(base_url.strip(), api_key, model.strip())
