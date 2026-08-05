"""Durable-memory to topic associations for the knowledge layer.

The durable view remains the source of truth.  This sidecar only records which
durable entries a generated topic covers, allowing retrieval and reconciliation
to be precise without parsing generated prose.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from ._files import atomic_write_text, safe_file_in_dir, sanitize_memory_text

if TYPE_CHECKING:
    from engine.llm.port import LLMPort

logger = logging.getLogger(__name__)


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
        files = state["files"]
        # A topic with no resolvable episode id has no page to retrieve, and
        # returning it here would misalign the parallel tuple that
        # ``file_ids_for_topics`` builds for the same topics.
        topics = [
            topic for topic, covered in state["topics"].items()
            if ids.intersection(covered) and (files.get(topic) or topic_filename(topic))
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

    def snapshot(self) -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
        """Return the persisted topic→entry-ids and topic→episode-id maps."""
        state = self._load()
        return (
            {topic: tuple(covered) for topic, covered in state["topics"].items()},
            dict(state["files"]),
        )

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

    def replace_topic(
        self,
        topic: str,
        durable_entries: list[str],
        *,
        file_id: str | None = None,
        page_hash: str | None = None,
    ) -> None:
        cleaned_topic = topic.strip()
        if not cleaned_topic:
            raise ValueError("topic must not be empty")
        state = self._load()
        covered = sorted({
            self.entry_id(entry) for entry in durable_entries if entry.strip()
        })
        filename = file_id or topic_filename(cleaned_topic)
        # Recording coverage without a filename left the two maps inconsistent,
        # which later made retrieval and reconciliation index a missing entry.
        if covered and filename:
            state["topics"][cleaned_topic] = covered
            state["files"][cleaned_topic] = filename.removesuffix(".md")
            if page_hash:
                state["hashes"][cleaned_topic] = page_hash
            else:
                state["hashes"].pop(cleaned_topic, None)
        else:
            state["topics"].pop(cleaned_topic, None)
            state["files"].pop(cleaned_topic, None)
            state["hashes"].pop(cleaned_topic, None)
        self._root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self._path,
            json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )

    def page_hashes(self) -> dict[str, str]:
        """Return topic → sha256 of the page content this store last generated.

        Used as a deletion guard: a page whose current content no longer matches
        the recorded hash was overwritten by something other than the topic sync
        (``memory_ops`` writes user episodes into the same namespace), so it is
        no longer ours to delete.
        """
        return dict(self._load()["hashes"])

    def replace_all_topics(
        self,
        topics: dict[str, list[str]],
        *,
        file_ids: dict[str, str] | None = None,
        file_hashes: dict[str, str] | None = None,
    ) -> dict[str, tuple[str, str | None]]:
        """Replace the full generated-topic snapshot and report removed topics.

        Returns removed topic → ``(episode_id, recorded_page_hash)``; a ``None``
        hash means the snapshot predates hash recording and the caller cannot
        verify provenance before deleting.
        """
        state = self._load()
        old_files = state["files"]
        old_hashes = state["hashes"]
        current = set(state["topics"])
        normalized: dict[str, list[str]] = {}
        resolved_files: dict[str, str] = {}
        resolved_hashes: dict[str, str] = {}
        for topic, entries in topics.items():
            cleaned = topic.strip()
            covered = sorted({self.entry_id(entry) for entry in entries if entry.strip()})
            file_id = (
                (file_ids or {}).get(cleaned)
                or old_files.get(cleaned)
                or topic_filename(cleaned)
            )
            if not cleaned or not covered or not file_id:
                continue
            normalized[cleaned] = covered
            resolved_files[cleaned] = file_id.removesuffix(".md")
            page_hash = (file_hashes or {}).get(cleaned) or old_hashes.get(cleaned)
            if page_hash:
                resolved_hashes[cleaned] = page_hash
        state["topics"] = normalized
        state["files"] = resolved_files
        state["hashes"] = resolved_hashes
        state["sync_pending"] = False
        self._root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self._path,
            json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        # Built after the write so a topic whose slug no longer resolves cannot
        # raise here and strand its generated page as an unreachable orphan.
        removed: dict[str, tuple[str, str | None]] = {}
        for topic in current.difference(normalized):
            stale_id = old_files.get(topic) or topic_filename(topic)
            if stale_id:
                removed[topic] = (stale_id.removesuffix(".md"), old_hashes.get(topic))
        return removed

    def _load(self) -> dict[str, object]:
        empty = {
            "version": self._VERSION,
            "topics": {},
            "files": {},
            "hashes": {},
            "sync_pending": False,
        }
        safe_path = safe_file_in_dir(self._root, self._path)
        if safe_path is None:
            return dict(empty)
        try:
            raw = json.loads(safe_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return dict(empty)
        topics = raw.get("topics") if isinstance(raw, dict) else None
        if not isinstance(topics, dict):
            return dict(empty)
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
        raw_hashes = raw.get("hashes") if isinstance(raw, dict) else {}
        hashes = {
            topic: page_hash for topic, page_hash in raw_hashes.items()
            if topic in normalized and isinstance(page_hash, str)
        } if isinstance(raw_hashes, dict) else {}
        return {
            "version": self._VERSION,
            "topics": normalized,
            "files": files,
            "hashes": hashes,
            "sync_pending": bool(raw.get("sync_pending", False)),
        }


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
        for file_id, page_hash in store.replace_all_topics({}).values():
            _remove_topic_file(memory_dir / "episodes", file_id, page_hash)
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
    if not groups:
        # An empty list is indistinguishable from a malformed response — a
        # classifier that answered ``{"topics": []}`` or misnamed the field used
        # to wipe every generated page in the snapshot.  Fail and let the caller
        # mark the sync pending instead.
        raise RuntimeError("topic classifier returned no usable topic groups")

    from .compile import compact_episode

    store = TopicAssociationStore(memory_dir)
    prior_topics, prior_files = store.snapshot()
    prior_hashes = store.page_hashes()
    episodes_dir = memory_dir / "episodes"
    updated: list[str] = []
    topic_entries: dict[str, list[str]] = {}
    file_ids: dict[str, str] = {}
    file_hashes: dict[str, str] = {}
    for topic, indexes in groups:
        selected = [entries[index] for index in indexes]
        covered = tuple(sorted({store.entry_id(entry) for entry in selected}))
        prior_file = prior_files.get(topic)
        # Regenerating an unchanged topic costs a generator (and reviewer) round
        # trip on every durable compile, so reuse the page that already exists.
        if (
            prior_topics.get(topic) == covered
            and prior_file
            and (episodes_dir / f"{prior_file}.md").is_file()
        ):
            topic_entries[topic] = selected
            file_ids[topic] = prior_file
            # Carry the recorded hash forward, NOT a hash of the current file: a
            # page the user overwrote since generation must keep mismatching so
            # the deletion guard still protects it.  A pre-hash snapshot has no
            # provenance to preserve, so adopt the current content once.
            prior_hash = prior_hashes.get(topic) or _page_content_hash(
                episodes_dir / f"{prior_file}.md"
            )
            if prior_hash:
                file_hashes[topic] = prior_hash
            updated.append(topic)
            continue
        related = [{"task": entry, "summary": entry} for entry in selected]
        path = await compact_episode(memory_dir, classifier, topic, related, reviewer=reviewer)
        if path is None:
            raise RuntimeError(f"topic episode generation failed: {topic}")
        topic_entries[topic] = selected
        file_ids[topic] = path.stem
        written_hash = _page_content_hash(path)
        if written_hash:
            file_hashes[topic] = written_hash
        updated.append(topic)
    removed = store.replace_all_topics(
        topic_entries, file_ids=file_ids, file_hashes=file_hashes
    )
    for file_id, page_hash in removed.values():
        _remove_topic_file(episodes_dir, file_id, page_hash)
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
    groups: dict[str, set[int]] = {}
    for item in topics[:8]:
        if not isinstance(item, dict):
            continue
        topic = item.get("topic")
        indexes = item.get("entries")
        if not isinstance(topic, str) or not isinstance(indexes, list):
            continue
        safe_topic, _, _ = sanitize_memory_text(topic)
        cleaned = safe_topic.strip()[:80]
        selected = {index for index in indexes if isinstance(index, int) and 0 <= index < entry_count}
        if cleaned and selected:
            # Classifiers occasionally repeat a topic name; merging keeps one
            # page per topic instead of paying a second generation (and review)
            # round with only the last duplicate's coverage surviving.
            groups.setdefault(cleaned, set()).update(selected)
    return [(topic, tuple(sorted(selected))) for topic, selected in groups.items()]


def _page_content_hash(path: Path) -> str | None:
    """Hash a topic page's exact bytes, or None when it cannot be read."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _remove_topic_file(
    episodes_dir: Path, file_id: str, expected_hash: str | None = None
) -> None:
    """Remove only the predictable generated topic file, never arbitrary paths.

    Generated pages share the episodes namespace with user episodes written by
    ``memory_ops``, and ``compact_episode`` overwrites a same-topic page in
    place.  When the snapshot recorded a content hash, delete only if the page
    still matches it: a mismatch means someone else's content now lives at this
    id, and removing it (plus its ``.bak``, the last copy) would destroy memory
    the user wrote.  Hash-less snapshots predate the guard and keep the old
    unconditional cleanup.
    """
    filename = f"{file_id.removesuffix('.md')}.md"
    if not file_id or "/" in file_id or "\\" in file_id:
        return
    page = episodes_dir / filename
    if expected_hash is not None and page.is_file():
        current = _page_content_hash(page)
        if current is not None and current != expected_hash:
            logger.info(
                "keeping topic page %s: content diverged from the generated snapshot",
                filename,
            )
            return
    # compact_episode keeps a .bak beside the page it overwrites; removing the
    # page must not strand that backup as an orphan.
    for name in (filename, f"{filename}.bak"):
        target = episodes_dir / name
        if target.is_file() and target.resolve().is_relative_to(episodes_dir.resolve()):
            target.unlink()
