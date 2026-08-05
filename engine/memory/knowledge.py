"""Durable-memory to topic associations for the knowledge layer.

The durable view remains the source of truth.  This sidecar only records which
durable entries a generated topic covers, allowing retrieval and reconciliation
to be precise without parsing generated prose.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from ._files import atomic_write_text, safe_file_in_dir, sanitize_memory_text

if TYPE_CHECKING:
    from engine.llm.port import LLMPort


def topic_filename(topic: str) -> str | None:
    slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", topic.lower()).strip("-_")[:60]
    return f"{slug}.md" if slug else None


class TopicAssociationStore:
    """Persist topic coverage by stable hashes of durable Markdown entries."""

    _STATE_NAME = "topic-associations.json"
    _VERSION = 1

    def __init__(self, memory_dir: Path) -> None:
        self._root = memory_dir / "episodes"
        self._path = self._root / self._STATE_NAME

    @staticmethod
    def entry_id(entry: str) -> str:
        return hashlib.sha256(entry.strip().encode("utf-8")).hexdigest()

    def topics_for_entries(self, entries: list[str]) -> tuple[str, ...]:
        ids = {self.entry_id(entry) for entry in entries if entry.strip()}
        if not ids:
            return ()
        state = self._load()
        topics = [
            topic for topic, covered in state["topics"].items()
            if ids.intersection(covered)
        ]
        return tuple(sorted(topics))

    def file_ids_for_topics(self, topics: tuple[str, ...]) -> tuple[str, ...]:
        """Return stored episode ids, including collision-safe writer suffixes."""
        state = self._load()
        files = state["files"]
        result: list[str] = []
        for topic in topics:
            fallback = topic_filename(topic)
            file_id = files.get(topic) or (fallback[:-3] if fallback else None)
            if isinstance(file_id, str):
                result.append(file_id)
        return tuple(result)

    def has_associations(self) -> bool:
        return bool(self._load()["topics"])

    def has_state(self) -> bool:
        return safe_file_in_dir(self._root, self._path) is not None

    def is_sync_pending(self) -> bool:
        return bool(self._load().get("sync_pending", False))

    def mark_sync_pending(self, pending: bool = True) -> None:
        state = self._load()
        state["sync_pending"] = pending
        self._root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self._path, json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n")

    def replace_topic(self, topic: str, durable_entries: list[str], *, file_id: str | None = None) -> None:
        cleaned_topic = topic.strip()
        if not cleaned_topic:
            raise ValueError("topic must not be empty")
        state = self._load()
        covered = sorted({
            self.entry_id(entry) for entry in durable_entries if entry.strip()
        })
        if covered:
            state["topics"][cleaned_topic] = covered
            filename = file_id or topic_filename(cleaned_topic)
            if filename:
                state["files"][cleaned_topic] = filename.removesuffix(".md")
        else:
            state["topics"].pop(cleaned_topic, None)
            state["files"].pop(cleaned_topic, None)
        self._root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self._path,
            json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )

    def replace_all_topics(
        self, topics: dict[str, list[str]], *, file_ids: dict[str, str] | None = None
    ) -> dict[str, str]:
        """Replace the full generated-topic snapshot and report removed topics."""
        state = self._load()
        current = set(state["topics"])
        normalized = {
            topic.strip(): sorted({self.entry_id(entry) for entry in entries if entry.strip()})
            for topic, entries in topics.items()
            if topic.strip() and entries
        }
        old_files = state["files"]
        state["topics"] = normalized
        state["files"] = {
            topic: (file_ids or {}).get(topic, old_files.get(topic, topic_filename(topic)[:-3]))
            for topic in normalized
            if topic_filename(topic) is not None
        }
        state["sync_pending"] = False
        self._root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self._path,
            json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        return {topic: old_files.get(topic, topic_filename(topic)[:-3]) for topic in current.difference(normalized)}

    def _load(self) -> dict[str, object]:
        safe_path = safe_file_in_dir(self._root, self._path)
        if safe_path is None:
            return {"version": self._VERSION, "topics": {}, "files": {}, "sync_pending": False}
        try:
            raw = json.loads(safe_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": self._VERSION, "topics": {}, "files": {}, "sync_pending": False}
        topics = raw.get("topics") if isinstance(raw, dict) else None
        if not isinstance(topics, dict):
            return {"version": self._VERSION, "topics": {}, "files": {}, "sync_pending": False}
        normalized = {
            topic: sorted({entry for entry in entries if isinstance(entry, str)})
            for topic, entries in topics.items()
            if isinstance(topic, str) and isinstance(entries, list)
        }
        raw_files = raw.get("files") if isinstance(raw, dict) else {}
        files = {
            topic: file_id for topic, file_id in raw_files.items()
            if topic in normalized and isinstance(file_id, str)
        } if isinstance(raw_files, dict) else {}
        return {"version": self._VERSION, "topics": normalized, "files": files, "sync_pending": bool(raw.get("sync_pending", False))}


async def sync_durable_topics(
    memory_dir: Path,
    classifier: "LLMPort",
    reviewer: "LLMPort | None" = None,
) -> tuple[str, ...]:
    """Create or refresh topic episodes from already-reviewed durable entries.

    Topic names are navigation metadata; episode prose still goes through the
    existing generation/review path in ``compact_episode``.
    """
    durable_path = safe_file_in_dir(memory_dir, memory_dir / "durable.md")
    if durable_path is None:
        return ()
    entries = _durable_entries(durable_path.read_text(encoding="utf-8"))
    if not entries:
        store = TopicAssociationStore(memory_dir)
        for file_id in store.replace_all_topics({}).values():
            _remove_topic_file(memory_dir / "episodes", file_id)
        return ()
    prompt = (
        "Group the following durable memory bullets into at most 8 concise domain "
        "topics. Return JSON only: {\"topics\":[{\"topic\":\"...\",\"entries\":[0]}]}. "
        "Every entries value must refer only to a supplied bullet index. Omit weak "
        "or one-off topics.\n\n" + "\n".join(
            f"[{index}] {entry}" for index, entry in enumerate(entries)
        )
    )
    response = await classifier.chat([
        {"role": "system", "content": "You organize durable knowledge. Output valid JSON only."},
        {"role": "user", "content": prompt},
    ])
    groups = _parse_groups(response.text, len(entries))
    if groups is None:
        raise RuntimeError("topic classifier returned invalid output")

    from .compile import compact_episode

    store = TopicAssociationStore(memory_dir)
    updated: list[str] = []
    topic_entries: dict[str, list[str]] = {}
    file_ids: dict[str, str] = {}
    for topic, indexes in groups:
        related = [{"task": entries[index], "summary": entries[index]} for index in indexes]
        path = await compact_episode(memory_dir, classifier, topic, related, reviewer=reviewer)
        if path is None:
            raise RuntimeError(f"topic episode generation failed: {topic}")
        topic_entries[topic] = [entries[index] for index in indexes]
        file_ids[topic] = path.stem
        updated.append(topic)
    removed = store.replace_all_topics(topic_entries, file_ids=file_ids)
    for file_id in removed.values():
        _remove_topic_file(memory_dir / "episodes", file_id)
    return tuple(updated)


def _durable_entries(text: str) -> list[str]:
    cleaned, _, _ = sanitize_memory_text(text)
    return [line.strip() for line in cleaned.splitlines() if line.lstrip().startswith("-")]


def _parse_groups(text: str, entry_count: int) -> list[tuple[str, tuple[int, ...]]] | None:
    try:
        raw = json.loads(text)
    except (TypeError, ValueError):
        return None
    topics = raw.get("topics") if isinstance(raw, dict) else None
    if not isinstance(topics, list):
        return None
    groups: list[tuple[str, tuple[int, ...]]] = []
    for item in topics[:8]:
        if not isinstance(item, dict):
            continue
        topic = item.get("topic")
        indexes = item.get("entries")
        if not isinstance(topic, str) or not isinstance(indexes, list):
            continue
        safe_topic, _, _ = sanitize_memory_text(topic)
        selected = tuple(sorted({index for index in indexes if isinstance(index, int) and 0 <= index < entry_count}))
        if safe_topic.strip() and selected:
            groups.append((safe_topic.strip()[:80], selected))
    return groups


def _remove_topic_file(episodes_dir: Path, file_id: str) -> None:
    """Remove only the predictable generated topic file, never arbitrary paths."""
    filename = f"{file_id.removesuffix('.md')}.md"
    if not file_id or "/" in file_id or "\\" in file_id:
        return
    target = episodes_dir / filename
    if target.is_file() and target.resolve().is_relative_to(episodes_dir.resolve()):
        target.unlink()
