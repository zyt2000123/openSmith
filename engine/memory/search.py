"""Memory search — FTS5 full-text index with trigram tokenizer.

Per-agent episode index under the profile memory dir
(`…/<id>/memory/episodes/search.sqlite`).
Uses trigram tokenizer for CJK (Chinese/Japanese/Korean) support.

Usage:
    idx = SearchIndex(episodes_dir)
    await idx.open()
    await idx.index_entry(id, content, scope)
    results = await idx.search("query", top_k=10)
    await idx.close()
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

import aiosqlite

_SCHEMA_VERSION = "2"

logger = logging.getLogger(__name__)

# The trigram tokenizer produces no tokens below three characters, so a term
# shorter than this can only be served by the LIKE fallback.
_TRIGRAM_MIN = 3
_MAX_TERMS = 16
# Hiragana, katakana, CJK ideographs and Hangul — the whole reason this index
# uses the trigram tokenizer.  Covering only ideographs left kana-only Japanese
# sentences on the unsliced whole-sentence path, i.e. still broken.
_CJK_CLASS = r"぀-ヿ一-鿿가-힯"
_CJK_RUN = re.compile(rf"[{_CJK_CLASS}]+")
_TOKEN_RUN = re.compile(rf"[{_CJK_CLASS}]+|[^\s{_CJK_CLASS}]+")
# Latin punctuation plus the fullwidth marks Chinese prose is full of.  A stray
# "，" surviving as a one-character term used to drag the entire query onto the
# LIKE path and undo the trigram slicing below.
_STRIP_CHARS = ".,;:!?()[]{}<>\"'`，。、；：！？（）【】「」『』《》〈〉～—…·"


def _search_terms(query: str) -> list[str]:
    """Split a query into terms the trigram index can actually match.

    CJK text carries no spaces, so ``str.split()`` hands back a whole sentence as
    a single term and the trigram tokenizer then demands that entire sentence
    appear verbatim in the corpus — which never happens, so every Chinese
    sentence query matched nothing at all.  Slice long CJK runs into overlapping
    trigrams so each piece is independently matchable, and let bm25 rank the
    documents that hit the most pieces.

    Slices are taken round-robin across runs rather than in reading order.  A
    single 18-character CJK run yields enough trigrams to fill ``_MAX_TERMS`` on
    its own, which would silently discard every later word in the query — the old
    whole-sentence term could never hit that cap, so the slicing introduced the
    truncation risk along with the fix.
    """
    groups: list[list[str]] = []
    for run in _TOKEN_RUN.findall(query):
        if _CJK_RUN.fullmatch(run):
            if len(run) <= _TRIGRAM_MIN:
                groups.append([run])
            else:
                groups.append([
                    run[index:index + _TRIGRAM_MIN]
                    for index in range(len(run) - _TRIGRAM_MIN + 1)
                ])
        else:
            cleaned = run.strip(_STRIP_CHARS)
            if cleaned:
                groups.append([cleaned])

    terms: list[str] = []
    seen: set[str] = set()
    for index in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if index >= len(group) or group[index] in seen:
                continue
            seen.add(group[index])
            terms.append(group[index])
            if len(terms) >= _MAX_TERMS:
                return terms
    return terms


class SearchIndex:
    def __init__(self, memory_dir: Path) -> None:
        self._db_path = memory_dir / "search.sqlite"
        self._version_path = memory_dir / ".fts_version"
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        needs_rebuild = self._needs_rebuild()
        try:
            await self._open_database(needs_rebuild)
        except sqlite3.DatabaseError as exc:
            if not self._is_corrupt_database_error(exc):
                raise
            logger.warning("memory search index is corrupt; rebuilding derived index", exc_info=True)
            self._discard_derived_index()
            await self._open_database(needs_rebuild=True)

    async def _open_database(self, needs_rebuild: bool) -> None:
        self._db = await aiosqlite.connect(str(self._db_path))
        try:
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA busy_timeout=3000")
            if needs_rebuild:
                await self._db.execute("DROP TABLE IF EXISTS memory_fts")
            await self._db.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts
                USING fts5(entry_id, content, scope, tokenize='trigram')
            """)
            await self._db.commit()
            if needs_rebuild:
                self._version_path.write_text(_SCHEMA_VERSION, encoding="utf-8")
                for state_name in (".index_mtime", ".index_state.json"):
                    (self._db_path.parent / state_name).unlink(missing_ok=True)
        except BaseException:
            await self._db.close()
            self._db = None
            raise

    def _discard_derived_index(self) -> None:
        """Remove only disposable SQLite artifacts before rebuilding them."""
        for suffix in ("", "-wal", "-shm"):
            self._db_path.with_name(f"{self._db_path.name}{suffix}").unlink(missing_ok=True)
        self._version_path.unlink(missing_ok=True)

    @staticmethod
    def _is_corrupt_database_error(exc: sqlite3.DatabaseError) -> bool:
        message = str(exc).lower()
        return "not a database" in message or "database disk image is malformed" in message

    def _needs_rebuild(self) -> bool:
        if not self._db_path.exists():
            return True
        if not self._version_path.exists():
            return True
        try:
            return self._version_path.read_text(encoding="utf-8").strip() != _SCHEMA_VERSION
        except OSError:
            return True

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def index_entry(self, entry_id: str, content: str, scope: str) -> None:
        if not self._db:
            return
        await self._db.execute("DELETE FROM memory_fts WHERE entry_id = ?", (entry_id,))
        await self._db.execute(
            "INSERT INTO memory_fts (entry_id, content, scope) VALUES (?, ?, ?)",
            (entry_id, content, scope),
        )
        await self._db.commit()

    async def remove_entry(self, entry_id: str) -> None:
        if not self._db:
            return
        await self._db.execute("DELETE FROM memory_fts WHERE entry_id = ?", (entry_id,))
        await self._db.commit()

    async def remove_missing_entries(self, entry_ids: set[str], scope: str) -> None:
        """Drop index rows whose source files no longer exist."""
        if not self._db:
            return
        rows = await self._db.execute_fetchall(
            "SELECT entry_id FROM memory_fts WHERE scope = ?",
            (scope,),
        )
        stale_ids = [
            row["entry_id"]
            for row in rows
            if row["entry_id"] not in entry_ids
        ]
        if not stale_ids:
            return
        await self._db.executemany(
            "DELETE FROM memory_fts WHERE entry_id = ?",
            [(entry_id,) for entry_id in stale_ids],
        )
        await self._db.commit()

    async def search(self, query: str, top_k: int = 10) -> list[dict]:
        stripped = query.strip()
        if not self._db or not stripped:
            return []
        terms = _search_terms(stripped)
        if not terms:
            return []
        matchable = [term for term in terms if len(term) >= _TRIGRAM_MIN]
        try:
            if not matchable:
                # The trigram tokenizer matches nothing for terms shorter than 3
                # characters (common for CJK words) — fall back to an all-term
                # LIKE scan over the small episode corpus.  Only when *no* term
                # is long enough: falling back merely because one short token
                # slipped in (a greeting, a stray fullwidth comma) would re-impose
                # near-verbatim matching on the rest of the query.
                escaped_terms = [
                    term.replace("\\", "\\\\")
                    .replace("%", r"\%")
                    .replace("_", r"\_")
                    for term in terms
                ]
                predicates = " AND ".join(
                    r"content LIKE ? ESCAPE '\'" for _ in escaped_terms
                )
                rows = await self._db.execute_fetchall(
                    f"SELECT entry_id FROM memory_fts WHERE {predicates} LIMIT ?",
                    (*[f"%{term}%" for term in escaped_terms], top_k),
                )
                return [{"id": r["entry_id"], "score": 0.0} for r in rows]
            # OR, not AND: this retrieves *relevant* episodes, and a sentence
            # sliced into trigrams would never have every piece present at once.
            # bm25 floats the documents matching the most pieces to the top.
            safe_query = " OR ".join(
                '"' + term.replace('"', '""') + '"'
                for term in matchable
            )
            rows = await self._db.execute_fetchall(
                "SELECT entry_id, bm25(memory_fts) AS score "
                "FROM memory_fts WHERE memory_fts MATCH ? "
                "ORDER BY score LIMIT ?",
                (safe_query, top_k),
            )
        except Exception:
            logger.warning("memory search query failed", exc_info=True)
            return []
        return [{"id": r["entry_id"], "score": -r["score"]} for r in rows]
