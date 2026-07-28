"""Memory compilation — recent events + durable facts + episode summaries.

Four compilation targets:
  compile_context()  → ../context.md  (stable user collaboration memory)
  compile_recent()   → recent.md     (last 3-7 days, budget-capped)
  compile_durable()  → durable.md    (incremental merge of stable facts)
  compact_episode()  → episodes/*.md (on an explicit user summary request)
  assemble_memory()  → combined str  (for prompt injection)

Fingerprint caching: MD5 of input keys. Same input → skip compilation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
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
from ._review import (
    MemoryCompilationError,
    _generate_and_review,
    _generate_and_review_result,
    _llm_summarize,
    _truncate_source,
)
from .history import append_memory_history
from .policy import (
    DURABLE_MEMORY_KINDS,
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
MAX_RECENT_CHARS = _MEMORY_POLICY.view("recent").max_chars
MAX_DURABLE_CHARS = _MEMORY_POLICY.view("durable").max_chars
MAX_RECENT_SOURCE_CHARS = 24_000
MAX_DURABLE_SOURCE_CHARS = 16_000
MAX_EPISODE_SOURCE_CHARS = 16_000
MIN_WINDOW_DAYS, MAX_WINDOW_DAYS = _MEMORY_POLICY.view("recent").window_days
# Compilation is deferred from the interactive turn and may require several
# generator/reviewer calls. Keep a finite bound, but do not make a normal
# background request fail at the same 30-second budget as a chat turn.
_RECENT_REVIEW_TIMEOUT_SECONDS = 300.0
_DURABLE_REVIEW_TIMEOUT_SECONDS = 300.0

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


def _clear_recent_view(memory_dir: Path) -> bool:
    """Remove stale derived recent-memory artifacts without touching events."""
    removed = False
    recent_path = memory_dir / "recent.md"
    old_text = ""
    safe_recent = safe_file_in_dir(memory_dir, recent_path)
    if safe_recent is not None:
        old_text = safe_recent.read_text(encoding="utf-8")
        atomic_write_text(memory_dir / "recent.md.bak", old_text)

    for name in ("recent.md", ".fp_recent"):
        path = memory_dir / name
        if path.is_file() or path.is_symlink():
            path.unlink()
            removed = True
    if removed:
        append_memory_history(
            memory_dir,
            target="recent",
            policy_version=_MEMORY_POLICY.version,
            status="written",
            old_text=old_text,
            new_text="",
        )
    return removed


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
    for line in lines[offset:]:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries


def _read_offset(memory_dir: Path) -> int:
    offset_file = memory_dir / ".compile_offset"
    if offset_file.is_file():
        try:
            return max(0, int(offset_file.read_text().strip()))
        except (ValueError, OSError):
            pass
    return 0


def _write_offset(memory_dir: Path, offset: int) -> None:
    atomic_write_text(memory_dir / ".compile_offset", str(offset))


def _read_durable_offset(memory_dir: Path) -> int:
    """Read the durable-specific checkpoint, falling back during migration."""
    offset_file = memory_dir / ".durable_offset"
    if offset_file.is_file():
        try:
            return max(0, int(offset_file.read_text(encoding="utf-8").strip()))
        except (ValueError, OSError):
            pass
    return _read_offset(memory_dir)


def _write_durable_offset(memory_dir: Path, offset: int) -> None:
    atomic_write_text(memory_dir / ".durable_offset", str(offset))


def _total_lines(memory_dir: Path) -> int:
    recent = memory_dir / "recent.jsonl"
    if not recent.is_file():
        return 0
    return len(recent.read_text(encoding="utf-8").strip().splitlines())


def _parse_ts(ts: str) -> datetime | None:
    if not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _filter_by_time(entries: list[dict], after: datetime) -> list[dict]:
    return [
        e for e in entries
        if (_parse_ts(e.get("timestamp", "")) or datetime.min.replace(tzinfo=timezone.utc)) >= after
    ]


def _entries_to_source(
    entries: list[dict],
    summary_limit: int | None = None,
    source_limit: int | None = None,
) -> str:
    """Render events without losing normal-sized content.

    ``recent.jsonl`` keeps the durable event record. Limits here constrain only
    LLM input, and include an explicit marker when they need to apply.
    """
    lines = []
    for entry in entries:
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
        lines.append(
            f"- [{timestamp[:16]}]{metadata_suffix} {task}: {summary}"
        )

    source = "\n".join(lines)
    return _truncate_source(source, source_limit) if source_limit is not None else source


def _safe_source_metadata(value: object, *, limit: int = 500) -> str:
    """Render untrusted JSONL metadata as a bounded, prompt-safe scalar."""
    if value is None:
        return ""
    cleaned, _, _ = sanitize_memory_text(str(value))
    normalized = " ".join(cleaned.split())
    return _truncate_source(normalized, limit) if len(normalized) > limit else normalized


_RECENT_KINDS = {
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
        elif view == "recent":
            if kind in _RECENT_KINDS and (scope == "project" or kind in {"correction", "forget"}):
                selected.append(entry)
        elif view == "durable":
            if kind in DURABLE_MEMORY_KINDS and (
                scope == "project" or kind in {"correction", "remember", "forget"}
            ):
                selected.append(entry)
    return selected


_VIEW_COMPILER_SYSTEM_PROMPT = (
    "You are Smith's memory compiler. Follow the supplied canonical MemoryPolicy exactly. "
    "Return only the complete Markdown document for the requested target view."
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
Generate the complete `{spec.path.as_posix()}` memory view.

Current time (UTC): {current_time}

Canonical MemoryPolicy:
{policy.instructions_for(view, role="compiler")}

Current accepted Markdown:
{existing or "(empty)"}

Selected evidence:
{source}

Output only the complete Markdown document beginning with `# {spec.title}`.
"""


async def _generate_view(
    policy: MemoryPolicy,
    view: MemoryViewName,
    llm: "LLMPort",
    reviewer: "LLMPort | None",
    *,
    existing: str,
    source: str,
) -> tuple[str, int]:
    if reviewer is None:
        raise MemoryCompilationError(
            f"{view} compilation requires a reviewer model"
        )
    prompt = _build_view_prompt(policy, view, existing=existing, source=source)
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
    )
    return _normalize_markdown(outcome.text), outcome.rounds


def _normalize_markdown(text: str) -> str:
    return text.strip() + "\n" if text.strip() else ""


def _read_view(path: Path) -> str:
    if not path.is_file():
        return ""
    content, _, _ = sanitize_memory_text(path.read_text(encoding="utf-8"))
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
) -> None:
    validate_rendered_view(policy, view, draft)
    if contains_secret(draft) or contains_injection(draft):
        raise MemoryCompilationError(f"{view} compilation output contains unsafe content")

    target = resolve_view_path(policy, memory_dir.parent, view)
    if existing and existing != draft:
        atomic_write_text(target.with_name(f"{target.name}.bak"), existing)
    atomic_write_text(target, draft)
    append_memory_history(
        memory_dir,
        target=view,
        policy_version=policy.version,
        status="unchanged" if existing == draft and status == "written" else status,
        old_text=existing,
        new_text=draft,
        review_rounds=review_rounds,
        error=error,
    )


def _fallback_inline(value: object, limit: int) -> str:
    """Render already-sanitized event data as one bounded Markdown line."""
    cleaned, _, _ = sanitize_memory_text(str(value or ""))
    cleaned = " ".join(cleaned.split()).replace("#", "＃").replace("`", "'")
    return cleaned[:limit].rstrip(" .。；;")


def _fallback_date(value: object) -> str:
    parsed = _parse_ts(str(value or ""))
    return (parsed or datetime.now(timezone.utc)).date().isoformat()


def _fallback_recent_document(entries: list[dict]) -> str:
    """Build a safe, extractive recent view when the model pipeline is unavailable."""
    active_lines: list[str] = []
    outcome_lines: list[str] = []
    for entry in entries[-16:]:
        topic = _fallback_inline(entry.get("task"), 80) or "未命名工作"
        date = _fallback_date(entry.get("timestamp"))
        is_partial = entry.get("kind") == "partial_work"
        status = "未完成" if is_partial else "已记录"
        reason = _fallback_inline(entry.get("reason"), 80)
        reason_suffix = f"；原因：{reason}" if reason else ""
        active_lines.append(
            f"- **{topic}** — 状态：{status}{reason_suffix}；"
            f"下一步：依据现有证据继续处理；更新：{date}。"
        )
        summary = _fallback_inline(entry.get("summary"), 180)
        if summary:
            evidence = _fallback_inline(entry.get("evidence"), 40) or "memory_event"
            outcome_lines.append(
                f"- **{topic}** — 结果：{summary}；证据：{evidence}。"
            )

    return "\n".join([
        "# Recent Working Memory",
        "",
        "## Active Work",
        *(active_lines or ["- **暂无近期工作** — 状态：空；下一步：无；更新：" + _fallback_date(None) + "。"]),
        "",
        "## Pending",
        "",
        "## Recent Verified Outcomes",
        *outcome_lines,
        "",
    ])


_DURABLE_FALLBACK_SECTIONS: tuple[str, ...] = (
    "Confirmed Facts",
    "Decisions",
    "Reusable Procedures",
    "Known Pitfalls",
)


def _existing_fallback_bullets(existing: str, section: str) -> list[str]:
    """Keep only bounded bullets from an accepted durable section."""
    in_section = False
    bullets: list[str] = []
    for line in existing.splitlines():
        if line.startswith("## "):
            in_section = line[3:].strip() == section
            continue
        if in_section and line.lstrip().startswith("-"):
            item = _fallback_inline(line.lstrip()[1:], 320)
            if item:
                bullets.append(f"- {item}")
    return bullets


def _fallback_durable_document(existing: str, entries: list[dict]) -> str:
    """Build a safe extractive durable view without inventing facts."""
    grouped = {
        section: _existing_fallback_bullets(existing, section)
        for section in _DURABLE_FALLBACK_SECTIONS
    }
    for entry in entries:
        topic = _fallback_inline(entry.get("task"), 80) or "未命名事实"
        summary = _fallback_inline(entry.get("summary"), 260)
        if not summary:
            continue
        evidence = _fallback_inline(entry.get("evidence"), 40) or "memory_event"
        kind = str(entry.get("kind") or "work")
        if kind == "decision":
            section = "Decisions"
            line = f"- **{topic}**: 决定 {summary}；适用范围：当前项目；证据：{evidence}。"
        elif kind == "correction":
            section = "Known Pitfalls"
            line = f"- **{topic}**: 记录已确认修正：{summary}；证据：{evidence}。"
        elif kind == "work":
            section = "Confirmed Facts"
            line = f"- **{topic}**: 已记录项目事实：{summary}；证据：{evidence}。"
        else:
            section = "Confirmed Facts"
            line = f"- **{topic}**: {summary}；证据：{evidence}。"
        if line not in grouped[section]:
            grouped[section].append(line)

    parts = ["# Durable Project Memory"]
    for section in _DURABLE_FALLBACK_SECTIONS:
        parts.extend(["", f"## {section}", *grouped[section]])
    parts.append("")
    return "\n".join(parts)


def _can_use_compilation_fallback(exc: Exception) -> bool:
    """Fallback only for transient/review-loop failures, never policy violations."""
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(exc, MemoryCompilationError) and getattr(exc, "review_rounds", 0) > 0


def _record_compile_failure(
    memory_dir: Path,
    view: MemoryViewName,
    policy: MemoryPolicy,
    existing: str,
    exc: Exception,
) -> None:
    status = (
        "rejected"
        if isinstance(exc, (MemoryCompilationError, MemoryPolicyError))
        else "failed"
    )
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
    entries = _entries_for_view(_load_recent(memory_dir), "context")
    if not entries:
        return False

    target = resolve_view_path(policy, memory_dir.parent, "context")
    existing = _read_view(target)
    fp_file = memory_dir / ".fp_context"
    fp = _fingerprint([
        f"{entry.get('timestamp', '')}:{entry.get('kind', '')}:{entry.get('task', '')[:80]}"
        for entry in entries
    ])
    if _read_fp(fp_file) == fp and target.is_file():
        return False

    source = _entries_to_source(entries, source_limit=MAX_DURABLE_SOURCE_CHARS)
    try:
        draft, rounds = await asyncio.wait_for(
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
            draft=draft,
            review_rounds=rounds,
        )
    except Exception as exc:
        _record_compile_failure(memory_dir, "context", policy, existing, exc)
        raise

    _write_fp(fp_file, fp)
    return True


async def compile_recent(
    memory_dir: Path,
    llm: "LLMPort",
    reviewer: "LLMPort | None" = None,
) -> bool:
    """Compile recent events into recent.md. Budget-capped, elastic 3-7 day window."""
    policy = _MEMORY_POLICY
    now = datetime.now(timezone.utc)
    # ponytail: recent.md is a rolling window — must read ALL events, not just
    # new ones. Offset is only for compile_durable's incremental merge.
    all_entries = _load_recent(memory_dir, from_offset=False)

    entries = _entries_for_view(
        _filter_by_time(all_entries, now - timedelta(days=MIN_WINDOW_DAYS)),
        "recent",
    )
    if not entries:
        entries = _entries_for_view(
            _filter_by_time(all_entries, now - timedelta(days=MAX_WINDOW_DAYS)),
            "recent",
        )
    if not entries:
        return _clear_recent_view(memory_dir)

    out = resolve_view_path(policy, memory_dir.parent, "recent")
    fp_file = memory_dir / ".fp_recent"

    fp = _fingerprint([f"{e.get('timestamp', '')}:{e.get('task', '')[:50]}" for e in entries])
    if _read_fp(fp_file) == fp and out.is_file():
        return False

    existing = _read_view(out)
    source = _entries_to_source(entries, source_limit=MAX_RECENT_SOURCE_CHARS)
    try:
        draft, rounds = await asyncio.wait_for(
            _generate_view(
                policy,
                "recent",
                llm,
                reviewer,
                existing=existing,
                source=source,
            ),
            timeout=_RECENT_REVIEW_TIMEOUT_SECONDS,
        )
        _commit_view(
            policy,
            "recent",
            memory_dir,
            existing=existing,
            draft=draft,
            review_rounds=rounds,
        )
    except Exception as exc:
        if reviewer is None or not _can_use_compilation_fallback(exc):
            _record_compile_failure(memory_dir, "recent", policy, existing, exc)
            raise
        try:
            fallback = _fallback_recent_document(entries)
            _commit_view(
                policy,
                "recent",
                memory_dir,
                existing=existing,
                draft=fallback,
                review_rounds=getattr(exc, "review_rounds", 0),
                status="fallback",
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            _record_compile_failure(memory_dir, "recent", policy, existing, exc)
            raise
        _write_fp(fp_file, fp)
        return True

    _write_fp(fp_file, fp)
    return True


async def compile_durable(
    memory_dir: Path,
    llm: "LLMPort",
    reviewer: "LLMPort | None" = None,
) -> bool:
    """Incrementally merge new events into durable.md."""
    policy = _MEMORY_POLICY
    durable_offset = _read_durable_offset(memory_dir)
    all_entries = _load_recent(memory_dir, offset=durable_offset)
    entries = _entries_for_view(all_entries, "durable")
    total = _total_lines(memory_dir)

    out = resolve_view_path(policy, memory_dir.parent, "durable")
    fp_file = memory_dir / ".fp_durable"

    if not entries:
        if all_entries:
            _write_durable_offset(memory_dir, total)
        return False

    fp = _fingerprint([f"{e.get('timestamp', '')}:{e.get('task', '')[:50]}" for e in entries])
    if _read_fp(fp_file) == fp and out.is_file():
        if not (memory_dir / ".durable_offset").is_file():
            _write_durable_offset(memory_dir, total)
        return False

    existing = _read_view(out)
    new_source = _entries_to_source(
        entries,
        summary_limit=1000,
        source_limit=MAX_DURABLE_SOURCE_CHARS,
    )

    if not new_source.strip():
        return False

    try:
        draft, rounds = await asyncio.wait_for(
            _generate_view(
                policy,
                "durable",
                llm,
                reviewer,
                existing=existing,
                source=new_source,
            ),
            timeout=_DURABLE_REVIEW_TIMEOUT_SECONDS,
        )
        if not draft:
            raise MemoryCompilationError("durable compilation output was empty")
        _commit_view(
            policy,
            "durable",
            memory_dir,
            existing=existing,
            draft=draft,
            review_rounds=rounds,
        )
    except Exception as exc:
        if reviewer is None or not _can_use_compilation_fallback(exc):
            _record_compile_failure(memory_dir, "durable", policy, existing, exc)
            raise
        try:
            fallback = _fallback_durable_document(existing, entries)
            _commit_view(
                policy,
                "durable",
                memory_dir,
                existing=existing,
                draft=fallback,
                review_rounds=getattr(exc, "review_rounds", 0),
                status="fallback",
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            _record_compile_failure(memory_dir, "durable", policy, existing, exc)
            raise
        _write_fp(fp_file, fp)
        _write_durable_offset(memory_dir, total)
        return True

    _write_fp(fp_file, fp)
    _write_durable_offset(memory_dir, total)
    return True


# ---------------------------------------------------------------------------
# compact_episode: compress a completed topic into an episode file
# ---------------------------------------------------------------------------

async def compact_episode(
    memory_dir: Path,
    llm: "LLMPort",
    topic: str,
    related_entries: list[dict],
    reviewer: "LLMPort | None" = None,
) -> Path | None:
    """Generate an episode summary for a completed topic. Returns the file path."""
    if not related_entries:
        return None
    if contains_secret(topic) or contains_injection(topic):
        logger.warning("episode topic contains unsafe content — skipping write")
        return None

    episodes_dir = memory_dir / "episodes"
    episodes_dir.mkdir(parents=True, exist_ok=True)

    slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", topic.lower()).strip("-_")[:60]
    if not slug:
        return None

    episodes_root = episodes_dir.resolve()
    out = (episodes_dir / f"{slug}.md").resolve()
    if not out.is_relative_to(episodes_root):
        raise ValueError("episode path escaped its storage directory")

    source = _entries_to_source(
        related_entries[-20:],
        summary_limit=1200,
        source_limit=MAX_EPISODE_SOURCE_CHARS,
    )
    prompt = (
        f"Write a concise episode summary for the topic: {topic}\n\n"
        f"Include: background, process, key decisions, and outcome.\n"
        f"Max 800 chars.\n\n{source}"
    )
    if reviewer:
        summary = await _generate_and_review(llm, reviewer, prompt, source)
    else:
        summary = await _llm_summarize(llm, prompt)

    _MAX_EPISODE_CHARS = 800
    if len(summary) > _MAX_EPISODE_CHARS:
        logger.warning(
            "episode summary exceeded %s characters — skipping write",
            _MAX_EPISODE_CHARS,
        )
        raise MemoryCompilationError("episode summary exceeded character budget")

    if contains_secret(summary):
        logger.warning("episode output contains secrets — skipping write")
        return None
    if contains_injection(summary):
        logger.warning("episode output contains injection markers — skipping write")
        return None

    atomic_write_text(out, f"# {topic}\n\n{summary}\n")
    return out


# ---------------------------------------------------------------------------
# assemble_memory: combine compiled layers for prompt injection
# ---------------------------------------------------------------------------

def assemble_memory(
    memory_dir: Path,
    *,
    include_durable: bool = True,
    include_recent: bool = True,
) -> str:
    """Combine durable + recent into a single memory block for prompt injection.

    Episodes are not included here — they should be retrieved via FTS5
    based on the current query and injected separately.
    """
    sections = []

    enabled = {
        "durable.md": include_durable,
        "recent.md": include_recent,
    }
    for name in MEMORY_LAYER_FILES:
        if not enabled.get(name, True):
            continue
        path = safe_file_in_dir(memory_dir, memory_dir / name)
        if path is not None:
            content, _, _ = sanitize_memory_text(path.read_text(encoding="utf-8"))
            content = content.strip()
            if content:
                sections.append(content)

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# run_compilation: lifecycle entry point for the three formal views
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
    total = _total_lines(memory_dir)
    results = {"context": False, "recent": False, "durable": False}
    errors: dict[str, str] = {}
    try:
        results["context"] = await compile_context(memory_dir, llm, reviewer)
    except Exception:
        logger.warning("context-memory compilation failed", exc_info=True)
        errors["context"] = "context-memory compilation failed"
    try:
        results["recent"] = await compile_recent(memory_dir, llm, reviewer)
    except Exception:
        logger.warning("recent-memory compilation failed", exc_info=True)
        errors["recent"] = "recent-memory compilation failed"
    try:
        results["durable"] = await compile_durable(memory_dir, llm, reviewer)
    except Exception:
        logger.warning("durable-memory compilation failed", exc_info=True)
        errors["durable"] = "durable-memory compilation failed"
    if not errors and any(results.values()):
        _write_offset(memory_dir, total)
    # A successful layer is useful progress even when a later layer failed.
    # Only lifecycle callers opt into resetting their retry counter here; the
    # direct API keeps its strict raise-on-error behavior by default.
    partial_progress_is_safe = (
        allow_partial_progress
        and set(errors) == {"durable"}
        and results["recent"]
    )
    if errors and raise_on_error and not partial_progress_is_safe:
        raise RuntimeError("; ".join(errors.values()))
    if return_diagnostics:
        return {"results": results, "errors": errors}
    return results
