"""Optional local vector index for topic-scoped knowledge retrieval."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Protocol

from ._files import atomic_write_text, safe_file_in_dir


class EmbeddingProvider(Protocol):
    """Provider-neutral embedding seam owned by the application runtime."""

    model: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class TopicVectorIndex:
    """Small JSON-backed vector index; unavailable embeddings simply mean no hits."""

    _NAME = "vectors.json"

    def __init__(self, episodes_dir: Path) -> None:
        self._root = episodes_dir
        self._path = episodes_dir / self._NAME

    async def sync(
        self,
        documents: dict[str, str],
        provider: EmbeddingProvider,
        *,
        valid_topics: frozenset[str] | None = None,
    ) -> None:
        """Refresh the chunks for *documents*, merging with what is stored.

        Routing is per-query, so a call carries only the topics routed right
        now.  Rebuilding the whole file from them threw away every other
        topic's vectors — alternating queries between two topics re-embedded
        (a paid API call) on every switch.  Topics absent from *documents*
        keep their stored vectors; *valid_topics*, when given, prunes topics
        that left the association snapshot entirely.  An unchanged result
        skips the write — this runs on the per-query hot path.
        """
        state = self._load()
        if state.get("model") != provider.model:
            state = {}
        current = {
            self._chunk_id(topic, chunk): {"topic": topic, "text": chunk}
            for topic, text in documents.items()
            for chunk in _chunks(text)
        }
        existing = state.get("items", {})
        items: dict[str, dict] = {}
        for item_id, item in existing.items():
            if not isinstance(item, dict):
                continue
            topic = item.get("topic")
            if topic in documents:
                # This topic is being re-synced below; stale chunk ids of its
                # earlier content must not linger.
                continue
            if valid_topics is not None and topic not in valid_topics:
                continue
            if isinstance(item.get("text"), str) and isinstance(item.get("vector"), list):
                items[item_id] = item
        # Reuse a stored vector only when the chunk text is unchanged *and* the
        # stored vector survived.  A corrupted entry has to be re-embedded; the
        # earlier split dropped it from both branches and lost it permanently.
        stale: list[tuple[str, dict]] = []
        for item_id, item in current.items():
            prior = existing.get(item_id)
            if (
                isinstance(prior, dict)
                and prior.get("text") == item["text"]
                and isinstance(prior.get("vector"), list)
            ):
                items[item_id] = {**item, "vector": prior["vector"]}
            else:
                stale.append((item_id, item))
        vectors = await provider.embed([item["text"] for _, item in stale]) if stale else []
        if len(vectors) != len(stale):
            raise ValueError("embedding provider returned an unexpected vector count")
        items.update({
            item_id: {**item, "vector": vector}
            for (item_id, item), vector in zip(stale, vectors, strict=True)
        })
        if state.get("model") == provider.model and items == existing:
            return
        self._root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self._path, json.dumps({"model": provider.model, "items": items}, ensure_ascii=False))

    async def search(
        self, query: str, topics: tuple[str, ...], provider: EmbeddingProvider, top_k: int
    ) -> list[dict[str, object]]:
        state = self._load()
        if state.get("model") != provider.model or not topics:
            return []
        vectors = await provider.embed([query])
        if len(vectors) != 1:
            return []
        query_vector = vectors[0]
        results = []
        for item in state.get("items", {}).values():
            if item.get("topic") not in topics or not isinstance(item.get("vector"), list):
                continue
            score = _cosine(query_vector, item["vector"])
            if score is not None:
                results.append({"topic": item["topic"], "text": item["text"], "score": score})
        return sorted(results, key=lambda item: float(item["score"]), reverse=True)[:top_k]

    @staticmethod
    def _chunk_id(topic: str, text: str) -> str:
        return hashlib.sha256(f"{topic}\0{text}".encode()).hexdigest()

    def _load(self) -> dict:
        path = safe_file_in_dir(self._root, self._path)
        if path is None:
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) and isinstance(raw.get("items", {}), dict) else {}


def _chunks(text: str, size: int = 700) -> list[str]:
    return [text[start:start + size] for start in range(0, len(text), size) if text[start:start + size].strip()]


def _cosine(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or not left:
        return None
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return None
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)
