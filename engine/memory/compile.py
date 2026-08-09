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
from ._review import (
    MemoryCompilationError,
    _generate_and_review_result,
    _truncate_source,
)
from .history import append_memory_history
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


def _write_offset(memory_dir: Path, offset: int) -> None:
    atomic_write_text(memory_dir / ".compile_offset", str(offset))


def _total_lines(memory_dir: Path) -> int:
    recent = memory_dir / "recent.jsonl"
    if not recent.is_file():
        return 0
    return len(recent.read_text(encoding="utf-8").strip().splitlines())


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


def _fallback_event_content(value: object, limit: int) -> str:
    """Recover candidate content from the internal ``[memory]`` envelope."""
    marker = "[memory] "
    content = _fallback_inline(value, limit + len(marker))
    if content.startswith(marker):
        content = content[len(marker):]
    return content[:limit].rstrip(" .。；;")


def _fallback_date(value: object) -> str:
    parsed = None
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        pass
    return (parsed or datetime.now(timezone.utc)).date().isoformat()


_DURABLE_FALLBACK_SECTIONS: tuple[str, ...] = _MEMORY_POLICY.view("durable").sections


# Sections that existed before the memory views were merged. The LLM compiler
# sees `existing` in full and can re-file them itself, but this deterministic
# extractor matches on heading names — without the mapping it would silently
# drop every bullet stored under a retired heading.
_LEGACY_DURABLE_SECTIONS: dict[str, str] = {
    "Confirmed Facts": "Verified Outcomes",
    "Reusable Procedures": "Verified Outcomes",
}


def _existing_fallback_bullets(existing: str, section: str) -> list[str]:
    """Keep only bounded bullets from an accepted durable section."""
    accepted = {section} | {
        legacy for legacy, current in _LEGACY_DURABLE_SECTIONS.items() if current == section
    }
    in_section = False
    bullets: list[str] = []
    for line in existing.splitlines():
        if line.startswith("## "):
            in_section = line[3:].strip() in accepted
            continue
        if in_section and line.lstrip().startswith("-"):
            # This text already came from the accepted, sanitized durable view.
            # Keep the complete bullet; the document-level eviction pass below
            # enforces the total budget without silently cutting facts in half.
            item = line.lstrip()[1:].strip()
            if item:
                bullets.append(f"- {item}")
    return bullets


def _fallback_durable_document(existing: str, entries: list[dict]) -> str:
    """Build a safe extractive durable view without inventing facts.

    Existing bullets are carried over first: this fallback runs on an
    *incremental* merge, so dropping them would silently erase every fact the
    reviewed pipeline had already accepted.
    """
    grouped = {
        section: _existing_fallback_bullets(existing, section)
        for section in _DURABLE_FALLBACK_SECTIONS
    }
    for entry in entries[-16:]:
        content = _fallback_event_content(entry.get("task"), 260)
        topic = content[:80].rstrip(" .。；;") or "未命名工作"
        summary = _fallback_inline(entry.get("summary"), 260)
        evidence = _fallback_inline(entry.get("evidence"), 40) or "memory_event"
        kind = str(entry.get("kind") or "work")
        if kind == "decision":
            decision = content or topic
            lines = [("Decisions", f"- **{topic}**: 决定 {decision}；适用范围：当前项目；证据：{evidence}。")]
        elif kind in {"correction", "pitfall"}:
            pitfall = content or topic
            lines = [("Known Pitfalls", f"- **{topic}**: 记录已确认陷阱：{pitfall}；证据：{evidence}。")]
        elif kind in {"work", "partial_work"}:
            date = _fallback_date(entry.get("timestamp"))
            status = "未完成" if kind == "partial_work" else "待复核"
            reason = _fallback_inline(entry.get("reason"), 80)
            reason_suffix = f"；原因：{reason}" if reason else ""
            lines = [(
                "Active Work",
                f"- **{topic}** — 状态：{status}{reason_suffix}；"
                f"下一步：依据现有证据继续处理；更新：{date}。",
            )]
            if kind == "work" and summary:
                lines.append(
                    (
                        "Pending",
                        f"- **{topic}** — 待处理：待复核本回合摘要“{summary}”；"
                        f"证据标签：{evidence}。",
                    )
                )
        elif content:
            # Structured memory candidates put the asserted content in
            # ``task`` and the supporting evidence description in ``summary``.
            # The latter must never be promoted into the asserted fact.
            lines = [("Verified Outcomes", f"- **{topic}** — 结果：{content}；证据：{evidence}。")]
        else:
            continue
        for section, line in lines:
            if line not in grouped[section]:
                grouped[section].append(line)

    # The fallback carries `existing` forward, so an almost-full durable view
    # would render over budget and _commit_view would reject it — the safety net
    # failing exactly when memory has accumulated enough to need it. Evict in the
    # order policy §5.1 prescribes until the document fits.
    document = _render_durable_fallback(grouped)
    for section in _DURABLE_EVICTION_ORDER:
        while len(document) > MAX_DURABLE_CHARS and grouped[section]:
            grouped[section].pop(0)  # oldest bullet in this section
            document = _render_durable_fallback(grouped)
    return document


# Least valuable first: transient status before verified conclusions, and
# decisions/pitfalls last because they are the entries worth keeping longest.
_DURABLE_EVICTION_ORDER: tuple[str, ...] = (
    "Active Work",
    "Pending",
    "Verified Outcomes",
    "Decisions",
    "Known Pitfalls",
)


def _render_durable_fallback(grouped: dict[str, list[str]]) -> str:
    parts = [f"# {_MEMORY_POLICY.view('durable').title}"]
    for section in _DURABLE_FALLBACK_SECTIONS:
        parts.extend(["", f"## {section}", *grouped[section]])
    parts.append("")
    return "\n".join(parts)


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

    # No qualifying evidence is a no-op, never a checkpoint advance. The
    # previous design skipped the log forward here whenever *any* event was
    # present, which silently discarded every event that did not qualify and
    # left durable.md permanently empty.
    if not entries:
        return False

    fp = _fingerprint([f"{e.get('timestamp', '')}:{e.get('task', '')[:50]}" for e in entries])
    if _read_fp(fp_file) == fp and out.is_file():
        return False

    existing = _read_view(out)
    source = _entries_to_source(
        entries,
        summary_limit=1000,
        source_limit=MAX_DURABLE_SOURCE_CHARS,
    )
    if not source.strip():
        return False

    try:
        draft, rounds = await asyncio.wait_for(
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
        requires_reviewed_merge = any(
            str(entry.get("kind") or "") in {"correction", "forget"}
            for entry in entries
        )
        if (
            reviewer is None
            or requires_reviewed_merge
            or not _can_use_compilation_fallback(exc)
        ):
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
        return True

    _write_fp(fp_file, fp)
    return True


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
    total = _total_lines(memory_dir)
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
    if not errors and any(results.values()):
        _write_offset(memory_dir, total)
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
