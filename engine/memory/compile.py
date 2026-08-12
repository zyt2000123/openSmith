"""Memory compilation — turn evidence into the two rendered memory views.

Two compilation targets:
  compile_context()  → ../context.md  (stable user collaboration memory)
  compile_durable()  → durable.md     (incremental merge of project memory)
  assemble_memory()  → combined str   (for prompt injection)

``durable.md`` has no time window: each run merges new evidence into the
existing document, so a fact survives until it is corrected, superseded, or
evicted for budget. Both views are injected in full — there is no query-time
retrieval layer, and therefore no index and no embeddings.

Fingerprint caching: MD5 of input keys. Same input → skip compilation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ._files import (
    MEMORY_LAYER_FILES,
    atomic_write_text,
    contains_injection,
    contains_secret,
    safe_file_in_dir,
    sanitize_memory_text,
)
from ._changeset import (
    EVICTION_ORDER,
    MemoryChange,
    apply_changes,
    evict_to_budget,
    parse_changeset,
    parse_document,
    render_changeset,
)
from ._guards import adjudicate, build_evidence_index
from ._review import (
    MemoryCompilationError,
    _generate_and_review_result,
    _parse_review_json,
    _truncate_source,
)
from ._snapshot import snapshot_views
from .history import append_memory_history, deferred_streak
from .policy import (
    MemoryPolicy,
    MemoryPolicyError,
    MemoryViewName,
    load_memory_policy,
    resolve_view_path,
    validate_rendered_view,
)

if TYPE_CHECKING:
    from engine.llm.port import LLMPort

_MEMORY_POLICY = load_memory_policy()
MAX_DURABLE_CHARS = _MEMORY_POLICY.view("durable").max_chars
MAX_DURABLE_SOURCE_CHARS = 24_000
# Compilation is deferred from the interactive turn and may require several
# generator/reviewer calls. Keep a finite bound, but do not make a normal
# background request fail at the same 30-second budget as a chat turn.
_DURABLE_REVIEW_TIMEOUT_SECONDS = 300.0
# Cycles with no applicable change after which the batch itself is treated as
# the problem and the cursor moves past it. See _skip_evidence_batch.
_MAX_DEFERRED_STREAK = 3

logger = logging.getLogger(__name__)


def _fingerprint(keys: list[str]) -> str:
    return hashlib.md5("|".join(keys).encode(), usedforsecurity=False).hexdigest()


def _read_fp(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""


def _write_fp(path: Path, fp: str) -> None:
    atomic_write_text(path, fp)


def _load_recent(
    memory_dir: Path,
    *,
    from_offset: bool = False,
    offset: int | None = None,
) -> list[dict]:
    """Load events from recent.jsonl.

    When *from_offset* is True, only return entries after the last
    successfully compiled offset (stored in ``.compile_offset``).
    """
    recent = memory_dir / "recent.jsonl"
    if not recent.is_file():
        return []
    lines = recent.read_text(encoding="utf-8").strip().splitlines()
    if offset is None:
        offset = _read_offset(memory_dir) if from_offset else 0
    entries = []
    for index, line in enumerate(lines[offset:], start=offset):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            # The log position travels with the event so the caller can advance
            # the compile cursor to what was actually consumed. Selection and
            # prompt-budget fitting both drop entries, and neither preserves a
            # count that maps back to lines on its own.
            parsed["_line"] = index
            entries.append(parsed)
    return entries


# Each view consumes the event log at its own pace, so each owns a cursor. One
# shared cursor made durable's progress speak for context's: Dream would reclaim
# a line durable had read and context had not.
_OFFSET_FILES: dict[str, str] = {
    "durable": ".compile_offset",
    "context": ".compile_offset_context",
}


def _read_offset(memory_dir: Path, view: str = "durable") -> int:
    offset_file = memory_dir / _OFFSET_FILES[view]
    if not offset_file.exists() and not offset_file.is_symlink():
        return 0
    if offset_file.is_symlink():
        raise OSError("compile offset is unavailable or unsafe")
    safe_path = safe_file_in_dir(memory_dir, offset_file)
    if safe_path is None:
        raise OSError("compile offset is unavailable or unsafe")
    try:
        offset = int(safe_path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeError, ValueError) as exc:
        raise OSError("compile offset is unavailable or unsafe") from exc
    if offset < 0:
        raise OSError("compile offset is unavailable or unsafe")
    return offset


def _write_offset(memory_dir: Path, offset: int, view: str = "durable") -> None:
    atomic_write_text(memory_dir / _OFFSET_FILES[view], str(offset))


def _total_lines(memory_dir: Path) -> int:
    recent = memory_dir / "recent.jsonl"
    if not recent.is_file():
        return 0
    return len(recent.read_text(encoding="utf-8").strip().splitlines())


def _entries_to_source(
    entries: list[dict],
    summary_limit: int | None = None,
    source_limit: int | None = None,
) -> tuple[str, int]:
    """Render events as prompt evidence, and report how many were included.

    ``recent.jsonl`` keeps the durable event record. Limits here constrain only
    LLM input.

    The budget selects a *prefix* of the events rather than eliding the middle of
    the joined text, and the count comes back with it. That is what keeps the
    compile cursor honest: the caller can only advance past evidence the model
    actually saw, and the remainder waits for the next cycle instead of being
    skipped unseen.
    """
    rendered: list[str] = []
    used = 0
    for entry in entries:
        line = _render_entry(entry, summary_limit)
        cost = len(line) + (1 if rendered else 0)
        if source_limit is not None:
            if rendered and used + cost > source_limit:
                break
            if not rendered and cost > source_limit:
                # One oversized event still has to be consumable, or the cursor
                # can never move past it and every later event starves behind it.
                line = _truncate_source(line, source_limit)
                cost = len(line)
        rendered.append(line)
        used += cost
    return "\n".join(rendered), len(rendered)


def _render_entry(entry: dict, summary_limit: int | None) -> str:
    task, _, _ = sanitize_memory_text(str(entry.get("task", "?")))
    summary, _, _ = sanitize_memory_text(str(entry.get("summary", "?")))
    if summary_limit is not None:
        summary = _truncate_source(summary, summary_limit)
    metadata_parts = []
    for key in ("kind", "scope", "evidence", "status", "reason"):
        value = _safe_source_metadata(entry.get(key))
        if value:
            metadata_parts.append(f"{key}={value}")
    metadata = ", ".join(metadata_parts)
    signals = entry.get("signals")
    if isinstance(signals, list):
        safe_signals = []
        for signal in signals:
            cleaned = _safe_source_metadata(signal)
            if cleaned.strip():
                safe_signals.append(cleaned.strip())
        if safe_signals:
            metadata = ", ".join(filter(None, (metadata, f"signals={safe_signals}")))
    metadata_suffix = f" ({metadata})" if metadata else ""
    timestamp = _safe_source_metadata(entry.get("timestamp", "?"), limit=64) or "?"
    return f"- [{timestamp[:16]}]{metadata_suffix} {task}: {summary}"


def _safe_source_metadata(value: object, *, limit: int = 500) -> str:
    """Render untrusted JSONL metadata as a bounded, prompt-safe scalar."""
    if value is None:
        return ""
    cleaned, _, _ = sanitize_memory_text(str(value))
    normalized = " ".join(cleaned.split())
    return _truncate_source(normalized, limit) if len(normalized) > limit else normalized


DURABLE_KINDS = {
    "work", "partial_work", "decision", "correction", "remember", "forget",
    "verified_fact", "procedure", "pitfall",
}


def _entries_for_view(entries: list[dict], view: MemoryViewName) -> list[dict]:
    """Select evidence for a view while remaining compatible with old events."""
    selected: list[dict] = []
    for entry in entries:
        kind = str(entry.get("kind") or "work")
        scope = str(entry.get("scope") or "project")
        if view == "context":
            if scope == "user":
                selected.append(entry)
        elif view == "durable":
            if kind in DURABLE_KINDS and (scope == "project" or kind in {"correction", "forget"}):
                selected.append(entry)
    return selected


_VIEW_COMPILER_SYSTEM_PROMPT = (
    "You are Smith's memory compiler. Follow the supplied canonical MemoryPolicy exactly. "
    "Return only a JSON change set for the requested target view. Never return a Markdown "
    "document: the document is rendered from your changes by deterministic code."
)


def _build_view_prompt(
    policy: MemoryPolicy,
    view: MemoryViewName,
    *,
    existing: str,
    source: str,
) -> str:
    spec = policy.view(view)
    current_time = datetime.now(timezone.utc).isoformat()
    return f"""\
Propose a change set for the `{spec.path.as_posix()}` memory view.

Current time (UTC): {current_time}

Canonical MemoryPolicy:
{policy.instructions_for(view, role="compiler")}

Current accepted Markdown (the trusted baseline; every bullet you do not name
survives untouched):
{existing or "(empty)"}

Selected evidence. Each line starts with its timestamp in square brackets: that
bracketed value is the `evidence.ref` for any change you base on that line, and
`evidence.quote` must be copied verbatim from the same line:
{source}

Legal `section` values for this view: {", ".join(spec.sections)}
Legal `view` value: {view}

Output only the JSON object described by the policy. If nothing in the evidence
is worth remembering, output {{"nothing_to_record": true, "changes": []}}.
"""


@dataclass(frozen=True)
class _ViewDraft:
    """A rendered view plus what the change set actually did to produce it."""

    document: str
    rounds: int
    applied: list[MemoryChange]
    notes: list[str]
    nothing_to_record: bool


def _build_draft(
    policy: MemoryPolicy,
    view: MemoryViewName,
    *,
    existing: str,
    raw: str,
    rounds: int,
    evidence: dict[str, list[str]],
) -> _ViewDraft:
    """Turn a model change set into a rendered document, deterministically.

    Rejections are collected rather than raised: one unusable edit must not
    discard the usable ones beside it, which is the whole reason the compiler
    emits changes instead of a replacement document.
    """
    spec = policy.view(view)
    changes, structural, nothing_to_record = parse_changeset(
        _parse_review_json(raw), view=view, sections=spec.sections
    )
    grouped = parse_document(
        existing or _empty_view_document(policy, view), spec.sections
    )
    # Adjudication before application: the three checks in policy 6.1 decide
    # whether a change is *entitled* to be applied, which is a question about the
    # evidence rather than about the document, so it is settled first.
    changes, unsupported = adjudicate(
        changes, view=view, evidence=evidence, grouped=grouped
    )
    updated, applied, rejected = apply_changes(grouped, changes)
    document, evicted = evict_to_budget(
        updated,
        title=spec.title,
        sections=spec.sections,
        order=EVICTION_ORDER[view],
        max_chars=spec.max_chars,
    )

    notes = [item.describe() for item in (*structural, *unsupported, *rejected)]
    notes.extend(f"evicted_for_budget: {bullet[:120]}" for bullet in evicted)

    if not applied and not nothing_to_record:
        # Nothing to write and no claim that nothing was worth writing: treat it
        # as a failed round so the caller leaves the accepted view untouched.
        detail = "; ".join(notes[:5]) or "empty change set"
        raise MemoryCompilationError(
            f"{view} change set produced no applicable change: {detail}",
            review_rounds=rounds,
        )
    return _ViewDraft(
        document=_normalize_markdown(document),
        rounds=rounds,
        applied=applied,
        notes=notes,
        nothing_to_record=nothing_to_record,
    )


async def _generate_view(
    policy: MemoryPolicy,
    view: MemoryViewName,
    llm: "LLMPort",
    reviewer: "LLMPort | None",
    *,
    existing: str,
    source: str,
) -> _ViewDraft:
    if reviewer is None:
        raise MemoryCompilationError(
            f"{view} compilation requires a reviewer model"
        )
    prompt = _build_view_prompt(policy, view, existing=existing, source=source)
    evidence = build_evidence_index(source)

    def pre_check(raw: str) -> tuple[list[str], str]:
        """Adjudicate a change set: blocking reasons, and what is left to review.

        Running the real pipeline is the check: ``_build_draft`` already raises
        exactly when no change survives parsing, adjudication and application, so
        there is one implementation of the rules rather than two that can drift.
        """
        try:
            draft = _build_draft(
                policy, view, existing=existing, raw=raw, rounds=0, evidence=evidence
            )
        except MemoryCompilationError as exc:
            return [str(exc)], raw
        return [], render_changeset(
            draft.applied, nothing_to_record=draft.nothing_to_record
        )

    review_source = (
        f"CURRENT TIME (UTC): {datetime.now(timezone.utc).isoformat()}\n\n"
        "PRIOR ACCEPTED MEMORY (reference state; retain only when the target "
        "policy permits it, and never use it alone to prove that recent work "
        f"is still current):\n{existing or '(empty)'}\n\n"
        f"SELECTED NEW EVIDENCE (ground truth):\n{source}"
    )
    outcome = await _generate_and_review_result(
        llm,
        reviewer,
        prompt,
        system_prompt=_VIEW_COMPILER_SYSTEM_PROMPT,
        source=review_source,
        target_view=f"{view}.md",
        review_policy=policy.instructions_for(view, role="reviewer"),
        pre_check=pre_check,
    )
    return _build_draft(
        policy,
        view,
        existing=existing,
        raw=outcome.text,
        rounds=outcome.rounds,
        evidence=evidence,
    )


def _normalize_markdown(text: str) -> str:
    return text.strip() + "\n" if text.strip() else ""


class MemoryViewUnreadableError(MemoryCompilationError):
    """An accepted view exists but could not be read back safely.

    Compilation must not continue on this: ``existing`` is what tells the
    model which facts are already recorded, so treating an unreadable view as
    an empty one produces a draft containing only the newest evidence -- and
    committing that silently replaces the whole document.
    """


def _read_view(path: Path) -> str:
    """Return the accepted view, refusing to report a wiped one as empty.

    ``sanitize_memory_text`` returns "" for the entire text when a secret or
    injection pattern matches only across a line break (``api_key:`` ending one
    line, ordinary prose beginning the next -- ``\\s`` spans the newline).  On a
    non-empty file that answer means "this could not be cleaned line by line",
    never "there was nothing here".  Dream already refuses to act on it when
    writing (see ``dream._sanitize_all_layers``); the read side must refuse too,
    because its caller overwrites the file rather than blanking it.
    """
    if not path.is_file():
        return ""
    raw = path.read_text(encoding="utf-8")
    content, secrets_removed, injections_removed = sanitize_memory_text(raw)
    if raw.strip() and not content.strip() and (secrets_removed or injections_removed):
        raise MemoryViewUnreadableError(
            f"{path.name} could not be sanitized without discarding the whole "
            "document; refusing to compile over it"
        )
    return _normalize_markdown(content)


def _commit_view(
    policy: MemoryPolicy,
    view: MemoryViewName,
    memory_dir: Path,
    *,
    existing: str,
    draft: str,
    review_rounds: int,
    status: str = "written",
    error: str | None = None,
    notes: list[str] | None = None,
) -> None:
    validate_rendered_view(policy, view, draft)
    if contains_secret(draft) or contains_injection(draft):
        raise MemoryCompilationError(f"{view} compilation output contains unsafe content")

    target = resolve_view_path(policy, memory_dir.parent, view)
    # Back up what is on disk rather than the caller's ``existing``.  The two
    # differ whenever the accepted view could not be read back in full, and
    # that is exactly when the overwrite is least recoverable -- an empty
    # ``existing`` used to skip the backup entirely.
    if target.is_file():
        on_disk = target.read_text(encoding="utf-8")
        if on_disk.strip() and on_disk != draft:
            atomic_write_text(target.with_name(f"{target.name}.bak"), on_disk)
    atomic_write_text(target, draft)
    recorded_status = (
        "unchanged" if existing == draft and status == "written" else status
    )
    append_memory_history(
        memory_dir,
        target=view,
        policy_version=policy.version,
        status=recorded_status,
        old_text=existing,
        new_text=draft,
        review_rounds=review_rounds,
        error=error,
        # Rejected edits and budget evictions are the memories that did *not*
        # get written.  Without them recorded, a memory that vanished is
        # indistinguishable from one that was never proposed.
        notes=notes,
    )
    # The audit record lands first: it is the durable trace, while the snapshot
    # is a recovery aid that may legitimately be unavailable.  ``.bak`` holds a
    # single generation, and history stores digests rather than text, so without
    # this two consecutive bad writes lose the last good document for good.
    snapshot_views(
        memory_dir.parent,
        f"memory: {view} ({recorded_status}, rounds={review_rounds})",
    )


def _empty_view_document(policy: MemoryPolicy, view: MemoryViewName) -> str:
    """Render a policy-valid empty Markdown view without inventing content."""
    spec = policy.view(view)
    parts = [f"# {spec.title}"]
    for section in spec.sections:
        parts.extend(("", f"## {section}"))
    return "\n".join(parts) + "\n"


def ensure_durable_template(memory_dir: Path) -> bool:
    """Create the canonical empty durable view when a profile has none yet."""
    policy = _MEMORY_POLICY
    out = resolve_view_path(policy, memory_dir.parent, "durable")
    if out.is_file():
        return False
    _commit_view(
        policy,
        "durable",
        memory_dir,
        existing="",
        draft=_empty_view_document(policy, "durable"),
        review_rounds=0,
        status="initialized",
    )
    return True


def _record_compile_failure(
    memory_dir: Path,
    view: MemoryViewName,
    policy: MemoryPolicy,
    existing: str,
    exc: Exception,
) -> None:
    if getattr(exc, "no_applicable_change", False):
        # Policy 9: nothing was applicable, so nothing was written. Kept distinct
        # from `rejected` because this is the only failure the skip counter may
        # act on -- see _skip_evidence_batch.
        status = "deferred"
    elif isinstance(exc, (MemoryCompilationError, MemoryPolicyError)):
        status = "rejected"
    else:
        status = "failed"
    append_memory_history(
        memory_dir,
        target=view,
        policy_version=policy.version,
        status=status,
        old_text=existing,
        review_rounds=getattr(exc, "review_rounds", 0),
        error=f"{type(exc).__name__}: {exc}",
    )


# ---------------------------------------------------------------------------
# Structured context/recent/durable views
# ---------------------------------------------------------------------------


async def compile_context(
    memory_dir: Path,
    llm: "LLMPort",
    reviewer: "LLMPort | None" = None,
) -> bool:
    """Compile user-scoped learning signals into ``agent_dir/context.md``."""
    policy = _MEMORY_POLICY
    # From its own cursor, not from line 0. Reading the whole log every time meant
    # the 24k prompt budget pinned context to the oldest evidence once the log
    # outgrew it, so newly stated preferences at the tail were never seen.
    entries = _entries_for_view(
        _load_recent(memory_dir, offset=_read_offset(memory_dir, "context")),
        "context",
    )
    if not entries:
        # Nothing unconsumed is user-scoped, so nothing here will ever reach
        # context.md. Move the cursor to the end of the log, or these lines block
        # Dream's reclamation forever.
        total = _total_lines(memory_dir)
        if total > _read_offset(memory_dir, "context"):
            _write_offset(memory_dir, total, "context")
        return False

    target = resolve_view_path(policy, memory_dir.parent, "context")
    try:
        existing = _read_view(target)
    except MemoryViewUnreadableError as exc:
        # Audit it here: the read happens before the compile try-block, so
        # without this the refusal would reach the log but never the history
        # that ``recent_failure_streak`` reports to the client.
        _record_compile_failure(memory_dir, "context", policy, "", exc)
        raise
    fp_file = memory_dir / ".fp_context"
    fp = _fingerprint([
        f"{entry.get('timestamp', '')}:{entry.get('kind', '')}:{entry.get('task', '')[:80]}"
        for entry in entries
    ])
    if _read_fp(fp_file) == fp and target.is_file():
        return False

    source, consumed = _entries_to_source(entries, source_limit=MAX_DURABLE_SOURCE_CHARS)
    consumed_through = int(entries[consumed - 1].get("_line", 0)) + 1
    try:
        draft = await asyncio.wait_for(
            _generate_view(
                policy,
                "context",
                llm,
                reviewer,
                existing=existing,
                source=source,
            ),
            timeout=_DURABLE_REVIEW_TIMEOUT_SECONDS,
        )
        _commit_view(
            policy,
            "context",
            memory_dir,
            existing=existing,
            draft=draft.document,
            review_rounds=draft.rounds,
            notes=draft.notes,
        )
    except Exception as exc:
        _record_compile_failure(memory_dir, "context", policy, existing, exc)
        raise

    _write_offset(memory_dir, consumed_through, "context")
    _write_fp(fp_file, fp)
    return True


async def compile_durable(
    memory_dir: Path,
    llm: "LLMPort",
    reviewer: "LLMPort | None" = None,
) -> bool:
    """Merge new events into durable.md — the single long-term project view.

    This is an incremental merge against ``.compile_offset``: only evidence the
    previous run did not consume is sent to the model, and the existing document
    is supplied as context so untouched facts survive. There is deliberately no
    time window — durable memory is never cleared just because a quiet period
    produced no events.
    """
    policy = _MEMORY_POLICY
    entries = _entries_for_view(_load_recent(memory_dir, from_offset=True), "durable")

    out = resolve_view_path(policy, memory_dir.parent, "durable")
    fp_file = memory_dir / ".fp_durable"
    ensure_durable_template(memory_dir)

    if not entries:
        # Nothing in the unconsumed span can ever become durable memory, so the
        # cursor moves to the end of the log. Leaving it behind would pin those
        # lines against Dream's reclamation forever, and durable will not learn
        # anything from them on a later pass either.
        total = _total_lines(memory_dir)
        if total > _read_offset(memory_dir):
            _write_offset(memory_dir, total)
        return False

    fp = _fingerprint([f"{e.get('timestamp', '')}:{e.get('task', '')[:50]}" for e in entries])
    if _read_fp(fp_file) == fp and out.is_file():
        return False

    try:
        existing = _read_view(out)
    except MemoryViewUnreadableError as exc:
        _record_compile_failure(memory_dir, "durable", policy, "", exc)
        raise
    source, consumed = _entries_to_source(
        entries,
        summary_limit=1000,
        source_limit=MAX_DURABLE_SOURCE_CHARS,
    )
    if not source.strip():
        return False
    # Only the events that fit into the prompt count as consumed. Advancing past
    # the rest would hand them to Dream for reclamation without any model having
    # read them.
    consumed_through = int(entries[consumed - 1].get("_line", 0)) + 1

    try:
        draft = await asyncio.wait_for(
            _generate_view(
                policy,
                "durable",
                llm,
                reviewer,
                existing=existing,
                source=source,
            ),
            timeout=_DURABLE_REVIEW_TIMEOUT_SECONDS,
        )
        if not draft.document:
            raise MemoryCompilationError("durable compilation output was empty")
        _commit_view(
            policy,
            "durable",
            memory_dir,
            existing=existing,
            draft=draft.document,
            review_rounds=draft.rounds,
            notes=draft.notes,
        )
    except Exception as exc:
        # No fallback document. Writing an unreviewed extractive merge poisons the
        # baseline: the next round reads it back as "the trusted current state"
        # and builds on it.  Nothing is written, neither checkpoint moves, and the
        # 10-turn compile interval is the throttle -- see policy 6.2.
        _record_compile_failure(memory_dir, "durable", policy, existing, exc)
        if deferred_streak(memory_dir, "durable") >= _MAX_DEFERRED_STREAK:
            _skip_evidence_batch(
                memory_dir, policy, existing=existing, through=consumed_through
            )
        raise

    _write_offset(memory_dir, consumed_through)
    _write_fp(fp_file, fp)
    return True


def _skip_evidence_batch(
    memory_dir: Path,
    policy: MemoryPolicy,
    *,
    existing: str,
    through: int,
) -> None:
    """Advance the cursor past evidence that keeps failing, writing no memory.

    Policy 6.2: after several cycles produce nothing applicable, the batch is
    what is stuck, and holding it forever means the log grows without bound and
    Dream can never reclaim anything.  Skipping moves only the cursor -- the
    events stay on disk under the normal retention window, and ``durable.md``
    keeps the last version that passed review.

    Counted from ``deferred`` records only -- cycles where nothing the model
    proposed was applicable.  An unsafe draft (``rejected``) or a provider outage
    (``failed``) never costs evidence: the streak resets on any other status,
    including the ``skipped`` record written here.
    """
    # The audit record lands before the cursor moves. The reverse order can
    # abandon a batch of evidence with no record of why, which is the one outcome
    # here that cannot be reconstructed; a trace of a skip whose cursor write
    # failed is merely repeated next cycle.
    append_memory_history(
        memory_dir,
        target="durable",
        policy_version=policy.version,
        status="skipped",
        old_text=existing,
        new_text=existing,
        notes=[f"skipped_evidence_through_line: {through}"],
    )
    _write_offset(memory_dir, through)


# ---------------------------------------------------------------------------
# assemble_memory: combine compiled layers for prompt injection
# ---------------------------------------------------------------------------

def assemble_memory(memory_dir: Path) -> str:
    """Read the rendered project memory block for prompt injection.

    durable.md is injected whole: it is budget-capped by policy, so there is
    nothing to retrieve and nothing to rank.
    """
    sections = []

    for name in MEMORY_LAYER_FILES:
        path = safe_file_in_dir(memory_dir, memory_dir / name)
        if path is not None:
            content, _, _ = sanitize_memory_text(path.read_text(encoding="utf-8"))
            content = content.strip()
            if content:
                sections.append(content)

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# run_compilation: lifecycle entry point for the two formal views
# ---------------------------------------------------------------------------

async def run_compilation(
    memory_dir: Path,
    llm: "LLMPort",
    *,
    reviewer: "LLMPort | None" = None,
    raise_on_error: bool = False,
    allow_partial_progress: bool = False,
    return_diagnostics: bool = False,
) -> dict:
    """Run compilation, optionally surfacing failures for retry control.

    Lifecycle maintenance may allow one layer to succeed while another layer
    remains pending review. Direct callers retain strict failure semantics by
    default.
    """
    memory_dir.mkdir(parents=True, exist_ok=True)
    results = {"context": False, "durable": False}
    errors: dict[str, str] = {}
    error_causes: dict[str, Exception] = {}
    try:
        results["context"] = await compile_context(memory_dir, llm, reviewer)
    except Exception as exc:
        logger.warning("context-memory compilation failed", exc_info=True)
        errors["context"] = "context-memory compilation failed"
        error_causes["context"] = exc
    try:
        results["durable"] = await compile_durable(memory_dir, llm, reviewer)
    except Exception as exc:
        logger.warning("durable-memory compilation failed", exc_info=True)
        errors["durable"] = "durable-memory compilation failed"
        error_causes["durable"] = exc
    # The compile cursor belongs to compile_durable: it is the only view that
    # reads from an offset, and it is the only place that knows how many events
    # actually fitted into the prompt. Advancing it here from a whole-file line
    # count was what let unread evidence be reclaimed.
    #
    # A successful layer is useful progress even when a later layer failed.
    # Only lifecycle callers opt into resetting their retry counter here; the
    # direct API keeps its strict raise-on-error behavior by default.
    partial_progress_is_safe = (
        allow_partial_progress
        and set(errors) == {"durable"}
        and results["context"]
    )
    if errors and raise_on_error and not partial_progress_is_safe:
        if len(error_causes) == 1:
            # Preserve the underlying failure (review rejection, policy error,
            # provider failure) so callers can distinguish retryable transport
            # errors from review/content rejections that deserve a fresh attempt.
            raise next(iter(error_causes.values()))
        raise RuntimeError("; ".join(errors.values()))
    if return_diagnostics:
        return {"results": results, "errors": errors}
    return results
