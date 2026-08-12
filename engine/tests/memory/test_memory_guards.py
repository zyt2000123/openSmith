"""Deterministic adjudication of a change set (policy 6.1).

Each case here is a claim a model can make about its own output that code refuses
to take on trust: an invented citation, an unjustified deletion, a verified
result resting on the assistant's own account of its work.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from engine.llm.client import ChatResponse
from engine.memory import compile as compile_module
from engine.memory._changeset import MemoryChange
from engine.memory._guards import adjudicate, build_evidence_index

from _changeset_fixtures import evidence_ref_and_quote
from engine.memory._review import MemoryCompilationError
from engine.memory.compile import (
    _read_offset,
    compile_durable,
    ensure_durable_template,
)

WORK_REF = "2026-08-12T10:00"
FACT_REF = "2026-08-12T11:00"
FORGET_REF = "2026-08-12T12:00"
PARTIAL_REF = "2026-08-12T13:00"

SOURCE = "\n".join((
    f"- [{WORK_REF}] (kind=work, scope=project) tune the loader: "
    "rewrote engine/memory/_files.py and startup dropped",
    f"- [{FACT_REF}] (kind=verified_fact, scope=project) loader benchmark: "
    "startup is 40ms, measured by pytest",
    f"- [{FORGET_REF}] (kind=forget, scope=project) drop the cache decision: "
    "user asked to forget the cache decision",
    f"- [{PARTIAL_REF}] (kind=partial_work, scope=project) index rebuild: "
    "half the shards are done",
))

EXISTING = {
    "Active Work": ["- **Index rebuild** — 状态：running；下一步：finish shards。"],
    "Pending": [],
    "Verified Outcomes": [],
    "Decisions": ["- **Cache**: 决定 keep the cache；适用范围：loader。"],
    "Known Pitfalls": [],
}


def _change(op: str, section: str, **kwargs: str) -> MemoryChange:
    kwargs.setdefault("view", "durable")
    return MemoryChange(op=op, section=section, **kwargs)  # type: ignore[arg-type]


def _judge(*changes: MemoryChange, view: str = "durable") -> tuple[list, list]:
    return adjudicate(
        list(changes),
        view=view,
        evidence=build_evidence_index(SOURCE),
        grouped={key: list(value) for key, value in EXISTING.items()},
    )


# ---------------------------------------------------------------------------
# Guard 1 — traceability
# ---------------------------------------------------------------------------

def test_a_fabricated_evidence_ref_is_refused() -> None:
    allowed, refused = _judge(_change(
        "add", "Decisions",
        content="- **Loader**: 决定 keep it；适用范围：memory。",
        evidence_ref="2099-01-01T00:00",
        evidence_quote="tune the loader",
    ))

    assert allowed == []
    assert refused[0].reason == "evidence_ref_not_in_batch"


def test_a_quote_absent_from_the_cited_event_is_refused() -> None:
    """The ref is real, the quote is not: citation without reading."""
    allowed, refused = _judge(_change(
        "add", "Decisions",
        content="- **Loader**: 决定 keep it；适用范围：memory。",
        evidence_ref=WORK_REF,
        evidence_quote="startup is 40ms, measured by pytest",  # that is FACT_REF
    ))

    assert allowed == []
    assert refused[0].reason == "evidence_quote_not_in_event"


def test_a_file_path_the_evidence_never_mentions_is_refused() -> None:
    allowed, refused = _judge(_change(
        "add", "Active Work",
        content="- **Loader** — 状态：patching engine/llm/router.py；下一步：test。",
        evidence_ref=WORK_REF,
        evidence_quote="tune the loader",
    ))

    assert allowed == []
    assert refused[0].reason == "content_anchor_not_in_evidence"
    assert "engine/llm/router.py" in refused[0].detail


def test_an_anchor_the_evidence_does_mention_is_allowed() -> None:
    change = _change(
        "add", "Active Work",
        content="- **Loader** — 状态：rewrote engine/memory/_files.py；下一步：test。",
        evidence_ref=WORK_REF,
        evidence_quote="rewrote engine/memory/_files.py and startup dropped",
    )

    allowed, refused = _judge(change)

    assert refused == []
    assert allowed == [change]


def test_replace_may_carry_an_anchor_over_from_the_bullet_it_rewrites() -> None:
    """An anchor already in the accepted view has passed review once.

    Requiring fresh evidence for it would make every status update on an
    existing bullet unrepeatable.
    """
    grouped = {
        "Active Work": ["- **Loader** — 状态：running `pytest -q`；下一步：wait。"],
        "Pending": [], "Verified Outcomes": [], "Decisions": [], "Known Pitfalls": [],
    }

    allowed, refused = adjudicate(
        [_change(
            "replace", "Active Work",
            target="Loader",
            content="- **Loader** — 状态：`pytest -q` is green；下一步：ship。",
            reason="progressed",
            evidence_ref=WORK_REF,
            evidence_quote="tune the loader",
        )],
        view="durable",
        evidence=build_evidence_index(SOURCE),
        grouped=grouped,
    )

    assert refused == []
    assert len(allowed) == 1


def test_an_entry_keeps_a_multiline_summary_attached_to_its_ref() -> None:
    """A summary containing a newline must not orphan its own quote."""
    index = build_evidence_index(
        f"- [{WORK_REF}] (kind=work) task: first line\nsecond line of the summary"
    )

    assert "second line of the summary" in index[WORK_REF][0]


# ---------------------------------------------------------------------------
# Guard 2 — retention
# ---------------------------------------------------------------------------

def test_a_settled_decision_needs_forget_or_correction_to_be_removed() -> None:
    allowed, refused = _judge(_change(
        "remove", "Decisions",
        target="Cache",
        reason="no longer relevant",
        evidence_ref=WORK_REF,
        evidence_quote="tune the loader",
    ))

    assert allowed == []
    assert refused[0].reason == "conclusion_changed_without_forget_or_correction"


def test_a_decision_cannot_be_inverted_by_rewriting_it_in_place() -> None:
    """`replace` must not be the cheap way around the retention guard.

    It keeps the topic key but may rewrite the whole body, so routing an inversion
    through replace erases the conclusion just as thoroughly as remove does -- and
    the new body is prose, which the traceability guard cannot check.
    """
    allowed, refused = _judge(_change(
        "replace", "Decisions",
        target="Cache",
        content="- **Cache**: 决定 不再使用缓存；适用范围：loader。",
        reason="the loader work suggests otherwise",
        evidence_ref=WORK_REF,
        evidence_quote="tune the loader",
    ))

    assert allowed == []
    assert refused[0].reason == "conclusion_changed_without_forget_or_correction"


def test_a_correction_may_rewrite_a_decision_in_place() -> None:
    change = _change(
        "replace", "Decisions",
        target="Cache",
        content="- **Cache**: 决定 drop the cache；适用范围：loader。",
        reason="user corrected it",
        evidence_ref=FORGET_REF,
        evidence_quote="user asked to forget the cache decision",
    )

    allowed, refused = _judge(change)

    assert refused == []
    assert allowed == [change]


def test_a_quote_too_short_to_show_the_entry_was_read_is_refused() -> None:
    """A one-character quote matches nearly any entry, so it proves nothing."""
    allowed, refused = _judge(_change(
        "add", "Active Work",
        content="- **Loader** — 状态：tuned；下一步：measure。",
        evidence_ref=WORK_REF,
        evidence_quote="the",
    ))

    assert allowed == []
    assert refused[0].reason == "evidence_quote_too_short"


def test_forget_evidence_authorizes_removing_a_decision() -> None:
    change = _change(
        "remove", "Decisions",
        target="Cache",
        reason="user asked to forget it",
        evidence_ref=FORGET_REF,
        evidence_quote="user asked to forget the cache decision",
    )

    allowed, refused = _judge(change)

    assert refused == []
    assert allowed == [change]


def test_active_work_can_be_removed_with_only_a_stated_reason() -> None:
    """In-flight status is churn, not a conclusion; completing it is enough."""
    change = _change(
        "remove", "Active Work",
        target="Index rebuild",
        reason="completed",
        evidence_ref=WORK_REF,
        evidence_quote="tune the loader",
    )

    allowed, refused = _judge(change)

    assert refused == []
    assert allowed == [change]


def test_a_deletion_with_no_reason_at_all_is_refused() -> None:
    allowed, refused = _judge(_change(
        "remove", "Active Work",
        target="Index rebuild",
        evidence_ref=WORK_REF,
        evidence_quote="tune the loader",
    ))

    assert allowed == []
    assert refused[0].reason == "deletion_without_reason"


# ---------------------------------------------------------------------------
# Guard 3 — placement
# ---------------------------------------------------------------------------

def test_partial_work_may_only_land_in_active_work() -> None:
    allowed, refused = _judge(_change(
        "add", "Decisions",
        content="- **Shards**: 决定 rebuild everything；适用范围：index。",
        evidence_ref=PARTIAL_REF,
        evidence_quote="half the shards are done",
    ))

    assert allowed == []
    assert refused[0].reason == "partial_work_outside_active_work"


def test_a_work_summary_cannot_establish_a_verified_outcome() -> None:
    """`work` is the assistant's own account, not a tool or test result."""
    allowed, refused = _judge(_change(
        "add", "Verified Outcomes",
        content="- **Loader** — 结果：startup dropped；证据：tool result。",
        evidence_ref=WORK_REF,
        evidence_quote="tune the loader",
    ))

    assert allowed == []
    assert refused[0].reason == "unverified_evidence_in_verified_outcomes"


def test_a_verified_outcome_must_state_its_evidence_field() -> None:
    allowed, refused = _judge(_change(
        "add", "Verified Outcomes",
        content="- **Loader** — 结果：startup is 40ms。",
        evidence_ref=FACT_REF,
        evidence_quote="startup is 40ms, measured by pytest",
    ))

    assert allowed == []
    assert refused[0].reason == "verified_outcome_without_evidence_field"


def test_verified_evidence_with_a_stated_field_is_allowed() -> None:
    change = _change(
        "add", "Verified Outcomes",
        content="- **Loader** — 结果：startup is 40ms；证据：benchmark。",
        evidence_ref=FACT_REF,
        evidence_quote="startup is 40ms, measured by pytest",
    )

    allowed, refused = _judge(change)

    assert refused == []
    assert allowed == [change]


def test_placement_does_not_constrain_the_context_view() -> None:
    """context.md sections carry no strength ordering, so there is nothing to rank."""
    change = _change(
        "add", "Confirmed Preferences",
        view="context",
        content="- **Language**: Chinese by default.",
        evidence_ref=PARTIAL_REF,
        evidence_quote="half the shards are done",
    )

    allowed, refused = _judge(change, view="context")

    assert refused == []
    assert allowed == [change]


def test_an_unlabelled_legacy_event_is_judged_as_work() -> None:
    """Pre-`kind` events render no kind at all; the weakest reading fails safe."""
    allowed, refused = adjudicate(
        [_change(
            "add", "Verified Outcomes",
            content="- **Old** — 结果：done；证据：log。",
            evidence_ref="2026-01-01T00:00",
            evidence_quote="legacy event",
        )],
        view="durable",
        evidence=build_evidence_index("- [2026-01-01T00:00] legacy event: no kind here"),
        grouped=dict(EXISTING),
    )

    assert allowed == []
    assert refused[0].reason == "unverified_evidence_in_verified_outcomes"


def test_one_unsupported_change_does_not_sink_the_supported_ones() -> None:
    good = _change(
        "add", "Active Work",
        content="- **Loader** — 状态：tuned；下一步：measure。",
        evidence_ref=WORK_REF,
        evidence_quote="tune the loader",
    )
    bad = _change(
        "add", "Decisions",
        content="- **Ghost**: 决定 something；适用范围：nowhere。",
        evidence_ref="2099-01-01T00:00",
        evidence_quote="invented",
    )

    allowed, refused = _judge(good, bad)

    assert allowed == [good]
    assert len(refused) == 1


# ---------------------------------------------------------------------------
# Wiring: adjudication runs before the reviewer, and the cursor stays honest
# ---------------------------------------------------------------------------

class _CountingLLM:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[list[dict]] = []

    async def chat(self, messages: list[dict], **_: object) -> ChatResponse:
        self.calls.append(messages)
        return ChatResponse(text=self.text)


def _write_events(memory_dir: Path, count: int, **overrides: object) -> None:
    memory_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for index in range(count):
        event = {
            "task": f"task {index}",
            "summary": "a" * 200,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "kind": "work",
            "scope": "project",
        }
        event.update(overrides)
        lines.append(json.dumps(event))
    (memory_dir / "recent.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


UNSUPPORTED = json.dumps({"changes": [{
    "op": "add",
    "section": "Decisions",
    "content": "- **Ghost**: 决定 invent this；适用范围：nowhere。",
    "evidence": {"ref": "2099-01-01T00:00", "quote": "never happened"},
}]})


def test_an_unsupported_change_set_never_reaches_the_reviewer(tmp_path: Path) -> None:
    """Deterministic refusal is free; a reviewer call is not."""
    memory_dir = tmp_path / "memory"
    _write_events(memory_dir, 1)
    generator = _CountingLLM(UNSUPPORTED)
    reviewer = _CountingLLM('{"pass": true, "hard_fail": [], "soft_fail": []}')

    with pytest.raises(MemoryCompilationError, match="evidence_ref_not_in_batch"):
        asyncio.run(compile_durable(memory_dir, generator, reviewer))

    assert reviewer.calls == []
    # Three generator attempts, each told what failed in machine-checkable terms.
    assert len(generator.calls) == 3
    assert "DETERMINISTIC ADJUDICATION" in generator.calls[1][-1]["content"]
    assert _read_offset(memory_dir) == 0


def test_the_reviewer_is_shown_only_the_changes_that_survived(tmp_path: Path) -> None:
    """A refused change must not be put in front of the reviewer.

    Otherwise the reviewer can hard-fail the batch over a fabricated change that
    was never going to be written, and take the supported changes down with it.
    """
    memory_dir = tmp_path / "memory"
    _write_events(memory_dir, 1)
    mixed = json.dumps({"changes": [
        {
            "op": "add", "section": "Decisions",
            "content": "- **Ghost**: 决定 invent this；适用范围：nowhere。",
            "evidence": {"ref": "2099-01-01T00:00", "quote": "never happened"},
        },
        {
            "op": "add", "section": "Active Work",
            "content": "- **Real** — 状态：running；下一步：measure。",
            "evidence": {"ref": "", "quote": ""},
        },
    ]}, ensure_ascii=False)

    class _Mixed(_CountingLLM):
        async def chat(self, messages: list[dict], **_: object) -> ChatResponse:
            self.calls.append(messages)
            prompt = messages[-1]["content"]
            ref, quote = evidence_ref_and_quote(prompt)
            payload = json.loads(mixed)
            payload["changes"][1]["evidence"] = {"ref": ref, "quote": quote}
            return ChatResponse(text=json.dumps(payload, ensure_ascii=False))

    reviewer = _CountingLLM('{"pass": true, "hard_fail": [], "soft_fail": []}')
    assert asyncio.run(compile_durable(memory_dir, _Mixed(""), reviewer)) is True

    shown = reviewer.calls[0][-1]["content"]
    assert "**Real**" in shown
    assert "Ghost" not in shown


def test_the_cursor_only_advances_past_events_that_fitted_the_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evidence the model never saw must not become reclaimable.

    The source budget used to elide the middle of the joined text and then the
    cursor jumped to the end of the whole log, so those events were dropped
    unread.
    """
    memory_dir = tmp_path / "memory"
    _write_events(memory_dir, 4)
    monkeypatch.setattr(compile_module, "MAX_DURABLE_SOURCE_CHARS", 260)

    from _changeset_fixtures import changeset_from_document

    class _Doc(_CountingLLM):
        async def chat(self, messages: list[dict], **_: object) -> ChatResponse:
            self.calls.append(messages)
            return ChatResponse(text=changeset_from_document(
                "# Durable Project Memory\n\n## Active Work\n"
                "- **Loader** — 状态：tuned；下一步：measure。\n",
                messages[-1]["content"],
            ))

    assert asyncio.run(compile_durable(
        memory_dir, _Doc(""), _CountingLLM('{"pass": true, "hard_fail": [], "soft_fail": []}')
    )) is True

    offset = _read_offset(memory_dir)
    assert 0 < offset < 4


def test_three_rejected_cycles_skip_the_batch_without_writing_memory(
    tmp_path: Path,
) -> None:
    """Policy 6.2: stuck evidence gives up the cursor, never the document."""
    memory_dir = tmp_path / "memory"
    _write_events(memory_dir, 2)
    # A run that has already compiled once: the template exists, so its
    # `initialized` audit record is not sitting between the rejections.
    ensure_durable_template(memory_dir)
    history = memory_dir / "memory_history.jsonl"
    history.write_text(
        "".join(
            json.dumps({"target": "durable", "status": "deferred"}) + "\n"
            for _ in range(2)
        ),
        encoding="utf-8",
    )
    generator = _CountingLLM(UNSUPPORTED)
    reviewer = _CountingLLM('{"pass": true, "hard_fail": [], "soft_fail": []}')

    with pytest.raises(MemoryCompilationError):
        asyncio.run(compile_durable(memory_dir, generator, reviewer))

    assert _read_offset(memory_dir) == 2
    records = [
        json.loads(line)
        for line in history.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records[-1]["status"] == "skipped"
    # The accepted view is still the empty template, never a degraded draft.
    written = (memory_dir / "durable.md").read_text(encoding="utf-8")
    assert "Ghost" not in written


def test_a_failed_cycle_leaves_the_fingerprint_alone(tmp_path: Path) -> None:
    """A written fingerprint means "this batch is done"; a failure must not claim that.

    Writing it on failure would make the next cycle skip the same evidence as
    already-compiled, so a single bad round would discard the batch silently.
    """
    memory_dir = tmp_path / "memory"
    _write_events(memory_dir, 1)

    with pytest.raises(MemoryCompilationError):
        asyncio.run(compile_durable(
            memory_dir, _CountingLLM(UNSUPPORTED),
            _CountingLLM('{"pass": true, "hard_fail": [], "soft_fail": []}'),
        ))

    assert not (memory_dir / ".fp_durable").exists()

    # And the retry can still land, because nothing was marked consumed.
    from _changeset_fixtures import changeset_from_document

    class _Doc(_CountingLLM):
        async def chat(self, messages: list[dict], **_: object) -> ChatResponse:
            self.calls.append(messages)
            return ChatResponse(text=changeset_from_document(
                "# Durable Project Memory\n\n## Active Work\n"
                "- **Loader** — 状态：tuned；下一步：measure。\n",
                messages[-1]["content"],
            ))

    assert asyncio.run(compile_durable(
        memory_dir, _Doc(""),
        _CountingLLM('{"pass": true, "hard_fail": [], "soft_fail": []}'),
    )) is True
    assert "**Loader**" in (memory_dir / "durable.md").read_text(encoding="utf-8")


def test_nothing_worth_recording_is_a_success_not_a_failure(tmp_path: Path) -> None:
    """A quiet batch genuinely has nothing to remember.

    Counting an honest blank as failure would make a working pipeline look broken
    and stall the cursor behind evidence that will never yield a memory.
    """
    memory_dir = tmp_path / "memory"
    _write_events(memory_dir, 2)
    blank = _CountingLLM(json.dumps({"nothing_to_record": True, "changes": []}))

    assert asyncio.run(compile_durable(
        memory_dir, blank,
        _CountingLLM('{"pass": true, "hard_fail": [], "soft_fail": []}'),
    )) is True

    assert _read_offset(memory_dir) == 2
    history = [
        json.loads(line)
        for line in (memory_dir / "memory_history.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ][-1]
    assert history["status"] == "unchanged"


def test_dream_will_not_reclaim_evidence_context_has_not_read(tmp_path: Path) -> None:
    """Each view owns a cursor; reclamation stops at whichever is further behind.

    One shared cursor let durable's progress speak for context's, so a stretch of
    continuously failing context compilation ended with user-scope evidence
    reclaimed before context.md ever absorbed it.
    """
    from engine.memory.dream import run_dream

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "recent.jsonl").write_text(
        "".join(
            json.dumps({
                "task": f"task {index}",
                "summary": "s",
                "timestamp": "2020-01-01T00:00:00+00:00",
            }) + "\n"
            for index in range(4)
        ),
        encoding="utf-8",
    )
    (memory_dir / "durable.md").write_text("exists", encoding="utf-8")
    # durable consumed everything; context is still at the start.
    (memory_dir / ".compile_offset").write_text("4", encoding="utf-8")
    (memory_dir / ".compile_offset_context").write_text("1", encoding="utf-8")

    report = asyncio.run(run_dream(memory_dir, _CountingLLM("")))

    assert report.log_lines_cleaned == 1
    remaining = (memory_dir / "recent.jsonl").read_text(encoding="utf-8").strip()
    assert len(remaining.splitlines()) == 3
    # durable's cursor rebases down by the one reclaimed line, not to zero.
    assert (memory_dir / ".compile_offset").read_text(encoding="utf-8") == "3"


def test_a_provider_outage_does_not_burn_the_evidence_batch(tmp_path: Path) -> None:
    """Only content rejections count towards giving up on a batch.

    A 401 or a dead relay says nothing about the evidence, so it must not push
    the cursor past events that were never actually judged.
    """
    memory_dir = tmp_path / "memory"
    _write_events(memory_dir, 2)
    ensure_durable_template(memory_dir)
    (memory_dir / "memory_history.jsonl").write_text(
        "".join(
            json.dumps({"target": "durable", "status": "deferred"}) + "\n"
            for _ in range(2)
        ),
        encoding="utf-8",
    )

    class _Down(_CountingLLM):
        async def chat(self, messages: list[dict], **_: object) -> ChatResponse:
            raise RuntimeError("401 unauthorized")

    with pytest.raises(RuntimeError, match="401"):
        asyncio.run(compile_durable(memory_dir, _Down(""), _CountingLLM("{}")))

    assert _read_offset(memory_dir) == 0
