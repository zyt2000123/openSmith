"""The summary cutoff is an absolute message count, not a window index.

``context_summary_cutoff`` counts messages from the start of the session.  Two
call sites used it as if it indexed into a bounded window:

* ``_recent_history`` paged from the cutoff and then took the last 10 rows of
  that page — but ``get_messages`` caps a page at 200, so once a session grew
  past 200 messages beyond the cutoff the engine was handed rows
  ``cutoff+190..cutoff+199`` instead of the newest ones.
* ``_history_before_message`` sliced ``prior_rows[cutoff:]`` onto a list already
  bounded to 10 rows, so any cutoff >= 10 emptied it and a resumed run saw the
  summary and nothing else.

The repo double here mirrors production's ``limit <= 0 -> 200`` default.  The
existing double omits it, which is exactly why neither defect was caught.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.session_service import SessionService  # noqa: E402


DEFAULT_PAGE = 200


class CountingSessionRepo:
    """Message store with production's paging semantics."""

    def __init__(self, total: int, cutoff: int) -> None:
        self.messages = [
            {"id": f"m{i}", "role": "user" if i % 2 == 0 else "assistant", "content": f"msg-{i}"}
            for i in range(total)
        ]
        self.context_summary = "summary of the earlier work"
        self.context_summary_cutoff = cutoff

    async def get_context(self, session_id: str) -> dict:
        return {
            "context_summary": self.context_summary,
            "context_summary_cutoff": self.context_summary_cutoff,
        }

    async def get_messages(
        self,
        session_id: str,
        limit: int = 0,
        offset: int = 0,
        max_content_bytes: int | None = None,
    ) -> list[dict]:
        effective = limit if limit > 0 else DEFAULT_PAGE
        return self.messages[offset : offset + effective]

    async def get_recent_messages(self, session_id: str, limit: int) -> list[dict]:
        return self.messages[-limit:] if limit > 0 else []

    async def get_messages_before(
        self, session_id: str, message_id: str, limit: int
    ) -> list[dict]:
        index = next(i for i, m in enumerate(self.messages) if m["id"] == message_id)
        start = max(0, index - limit)
        return self.messages[start:index]

    async def count_messages(self, session_id: str, before_message_id: str | None = None) -> int:
        if before_message_id is None:
            return len(self.messages)
        return next(i for i, m in enumerate(self.messages) if m["id"] == before_message_id)


class _NoProfiles:
    pass


def _service(repo) -> SessionService:
    return SessionService(repo, _NoProfiles())


def _contents(history: list[dict]) -> list[str]:
    return [h["content"] for h in history if not h["content"].startswith("[Session context")]


@pytest.mark.asyncio
async def test_recent_history_returns_the_newest_messages_past_200() -> None:
    """A long compressed session must still see its most recent turns."""
    repo = CountingSessionRepo(total=260, cutoff=10)

    history = await _service(repo)._recent_history("sess-1")

    assert "msg-259" in _contents(history), (
        f"engine was handed a stale window: {_contents(history)}"
    )


@pytest.mark.asyncio
async def test_recent_history_excludes_summarized_messages() -> None:
    """Messages the summary already covers must not be repeated."""
    repo = CountingSessionRepo(total=14, cutoff=10)

    contents = _contents(await _service(repo)._recent_history("sess-1"))

    assert contents == ["msg-10", "msg-11", "msg-12", "msg-13"]


@pytest.mark.asyncio
async def test_recent_history_without_compression_is_unchanged() -> None:
    repo = CountingSessionRepo(total=60, cutoff=0)
    repo.context_summary = ""

    contents = _contents(await _service(repo)._recent_history("sess-1"))

    assert contents[-1] == "msg-59"
    assert len(contents) == 10


@pytest.mark.asyncio
async def test_resume_history_keeps_recent_turns_in_a_compressed_session() -> None:
    """cutoff >= 10 used to empty the resume window entirely."""
    repo = CountingSessionRepo(total=60, cutoff=10)

    history = await _service(repo)._history_before_message("sess-1", "m59")

    assert _contents(history), "resume handed the engine zero recent messages"
    assert "msg-58" in _contents(history)


@pytest.mark.asyncio
async def test_resume_history_still_drops_summarized_rows() -> None:
    """Rows inside the summarized prefix stay excluded."""
    repo = CountingSessionRepo(total=20, cutoff=12)

    contents = _contents(await _service(repo)._history_before_message("sess-1", "m15"))

    assert contents == ["msg-12", "msg-13", "msg-14"]
