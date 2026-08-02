"""Periodic, evidence-bound candidate discovery for durable memory.

The nudge is deliberately *not* a durable-memory writer.  It periodically
reviews completed tool-backed work and can append structured candidate evidence
to ``recent.jsonl``.  The existing compiler and reviewer remain the only route
that can update ``durable.md``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ._files import (
    atomic_write_text,
    contains_injection,
    contains_secret,
    safe_file_in_dir,
    sanitize_memory_text,
)
from ._review import MemoryCompilationError, _generate_and_review_result
from .history import append_memory_history
from .policy import load_memory_policy

if TYPE_CHECKING:
    from engine.llm.port import LLMPort


logger = logging.getLogger(__name__)

# Keep this cadence distinct from the five-turn view compiler and the
# fifty-event Dream reconciler.  It is a scheduling interval, never evidence
# of a durable fact.
NUDGE_INTERVAL = 20

_NUDGE_OFFSET_FILE = ".nudge_offset"
_MAX_NUDGE_SOURCE_CHARS = 14_000
_MAX_CANDIDATES = 2
_MAX_CANDIDATE_CHARS = 800
_MAX_EVIDENCE_CHARS = 1_000
_ALLOWED_KINDS = frozenset({"decision", "verified_fact", "procedure", "pitfall"})
_ALLOWED_EVIDENCE_TYPES = frozenset({"tool_result"})
_TRANSIENT_CANDIDATE_PATTERN = re.compile(
    r"(?:\b(?:todo|to-do|current\s+(?:task|status)|next\s+step|"
    r"in\s+progress|task\s+plan)\b|待办|当前(?:任务|状态)|下一步|进行中|任务计划)",
    re.IGNORECASE,
)


class MemoryNudgeError(RuntimeError):
    """The nudge output is not safe enough to become candidate evidence."""


@dataclass(frozen=True)
class NudgeEvidence:
    """A bounded, sanitized batch of completed tool-backed work."""

    start_offset: int
    end_offset: int
    source: str
    excerpts: tuple[str, ...]
    error: str | None = None


@dataclass(frozen=True)
class NudgeCandidate:
    """One policy-shaped candidate, still awaiting normal durable compilation."""

    kind: str
    scope: str
    content: str
    evidence: str
    evidence_type: str


@dataclass(frozen=True)
class NudgeReport:
    """Outcome of one periodic quality review."""

    status: Literal["written", "unchanged", "rejected", "failed"]
    candidates_written: int = 0
    review_rounds: int = 0
    error: str | None = None

    @property
    def completed(self) -> bool:
        """Whether the cadence may safely advance past this evidence batch."""
        return self.status in {"written", "unchanged"}


_NUDGE_PROMPT = """\
## Periodic Memory Nudge

You are Smith's periodic quality curator. Review only the evidence package
below. Decide whether it contains up to {max_candidates} project facts that
will remain useful across future tasks.

Eligible kinds: decision, verified_fact, procedure, pitfall.

Do NOT create a candidate for:
- a task plan, current status, or one-off execution detail;
- a claim that is absent from the evidence package;
- a user preference, secret, command, or instruction to the agent.

Every candidate must use scope "project" and evidence_type "tool_result", and
copy an exact supporting excerpt from the evidence package into its evidence
field. Prefer no candidate when uncertain.

Return exactly one JSON object and nothing else:
{{"candidates":[{{"kind":"procedure","scope":"project","content":"...","evidence":"exact excerpt","evidence_type":"tool_result"}}]}}

Evidence package:
{source}
"""

_NUDGE_SYSTEM_PROMPT = (
    "You create candidate evidence for Smith's memory pipeline. "
    "Candidates are not durable facts. Never infer beyond the supplied evidence "
    "and output only the requested JSON object."
)

_NUDGE_REVIEW_POLICY = """\
Periodic nudge candidates must be evidence-bound and project-scoped.
Reject any candidate that is not supported by the supplied evidence, contains a
secret or instruction, represents a plan/current task, or is not one of the
allowed stable kinds. Empty candidate lists are valid and preferred over weak
claims."""


def _read_offset(memory_dir: Path) -> int:
    path = memory_dir / _NUDGE_OFFSET_FILE
    if not path.exists() and not path.is_symlink():
        return 0
    if path.is_symlink():
        raise OSError("nudge offset is unavailable or unsafe")
    safe_path = safe_file_in_dir(memory_dir, path)
    if safe_path is None:
        raise OSError("nudge offset is unavailable or unsafe")
    try:
        offset = int(safe_path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeError, ValueError) as exc:
        raise OSError("nudge offset is unavailable or unsafe") from exc
    if offset < 0:
        raise OSError("nudge offset is unavailable or unsafe")
    return offset


def _write_offset(memory_dir: Path, offset: int) -> None:
    path = memory_dir / _NUDGE_OFFSET_FILE
    if path.is_symlink():
        raise OSError("nudge offset is unavailable or unsafe")
    atomic_write_text(path, str(max(0, offset)))


def _bounded(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned, _, _ = sanitize_memory_text(value)
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit]


def _read_recent_lines(memory_dir: Path) -> list[str]:
    """Read recent.jsonl, repairing a torn trailing line from a crashed writer.

    Offset-advancing readers treat every line they pass as inspected.  A partial
    line left by an interrupted ``open("a")`` write would otherwise be silently
    consumed (and never re-emitted), so drop it from the file before parsing.
    """
    recent_path = memory_dir / "recent.jsonl"
    if not recent_path.exists() and not recent_path.is_symlink():
        return []
    recent = safe_file_in_dir(memory_dir, recent_path)
    if recent is None:
        raise OSError("recent.jsonl is unavailable or unsafe")
    try:
        raw = recent.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise OSError("recent.jsonl could not be read") from exc
    if not raw or raw.endswith("\n"):
        return raw.splitlines()
    # The writer was interrupted mid-line: repair the log by removing the torn
    # line so it can never be treated as inspected evidence.
    lines = raw.splitlines()
    if len(lines) > 1:
        atomic_write_text(recent, "\n".join(lines[:-1]) + "\n")
    else:
        atomic_write_text(recent, "")
    return lines[:-1]


def _load_evidence(memory_dir: Path) -> NudgeEvidence:
    try:
        lines = _read_recent_lines(memory_dir)
    except OSError as exc:
        return NudgeEvidence(0, 0, "", (), str(exc))

    try:
        stored_offset = _read_offset(memory_dir)
    except OSError as exc:
        return NudgeEvidence(0, 0, "", (), str(exc))
    start_offset = stored_offset if stored_offset <= len(lines) else 0
    source_parts: list[str] = []
    excerpts: list[str] = []
    used = 0
    end_offset = start_offset
    for line_number, line in enumerate(lines[start_offset:], start=start_offset):
        # Advance across every line that has been inspected, including entries
        # that are irrelevant to the nudge.  If the source budget is reached,
        # reset this to the first omitted line so it is retried next cycle.
        end_offset = line_number + 1
        try:
            entry = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        # Ordinary completed tool work is the only nudge input.  Explicit
        # decisions/corrections already enter the durable admission path.
        if entry.get("kind") != "work" or entry.get("evidence") != "tool_result":
            continue
        task = _bounded(entry.get("task"), 220)
        summary = _bounded(entry.get("summary"), 640)
        if not task or not summary:
            continue
        block = (
            f"[Evidence {line_number + 1}]\n"
            f"Task: {task}\n"
            f"Result: {summary}\n"
            "Evidence type: tool_result"
        )
        # Preserve all events in the batch while bounding the prompt.  An
        # omitted event is not marked reviewed and will be considered next run.
        if used + len(block) > _MAX_NUDGE_SOURCE_CHARS:
            end_offset = line_number
            break
        source_parts.append(block)
        excerpts.append(summary)
        used += len(block)

    return NudgeEvidence(
        start_offset,
        end_offset,
        "\n\n".join(source_parts),
        tuple(excerpts),
    )


def _parse_candidates(text: str, evidence: NudgeEvidence) -> list[NudgeCandidate]:
    try:
        payload = json.loads(text.strip())
    except (TypeError, ValueError) as exc:
        raise MemoryNudgeError("nudge output was not valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"candidates"}:
        raise MemoryNudgeError("nudge output must contain only a candidates list")
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list):
        raise MemoryNudgeError("nudge candidates must be a list")
    if len(raw_candidates) > _MAX_CANDIDATES:
        raise MemoryNudgeError("nudge returned too many candidates")

    candidates: list[NudgeCandidate] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            raise MemoryNudgeError("nudge candidate must be an object")
        expected = {"kind", "scope", "content", "evidence", "evidence_type"}
        if set(raw) != expected:
            raise MemoryNudgeError("nudge candidate fields do not match the contract")
        kind = raw.get("kind")
        scope = raw.get("scope")
        content = raw.get("content")
        excerpt = raw.get("evidence")
        evidence_type = raw.get("evidence_type")
        if kind not in _ALLOWED_KINDS or scope != "project":
            raise MemoryNudgeError("nudge candidate kind or scope is not allowed")
        if evidence_type not in _ALLOWED_EVIDENCE_TYPES:
            raise MemoryNudgeError("nudge candidate evidence type is not allowed")
        if not isinstance(content, str) or not isinstance(excerpt, str):
            raise MemoryNudgeError("nudge candidate content and evidence must be strings")
        content = content.strip()
        excerpt = excerpt.strip()
        if not content or len(content) > _MAX_CANDIDATE_CHARS:
            raise MemoryNudgeError("nudge candidate content is empty or too long")
        if _TRANSIENT_CANDIDATE_PATTERN.search(content):
            raise MemoryNudgeError("nudge candidate represented transient task state")
        if len(excerpt) < 12 or len(excerpt) > _MAX_EVIDENCE_CHARS:
            raise MemoryNudgeError("nudge candidate evidence is too short or too long")
        # The excerpt must bind to exactly one event block.  Substring
        # containment alone would let a short generic excerpt be attributed to a
        # different event whose summary happens to contain the same fragment.
        normalized_excerpt = " ".join(excerpt.split())
        matched_sources = [
            source_excerpt
            for source_excerpt in evidence.excerpts
            if normalized_excerpt in " ".join(source_excerpt.split())
        ]
        if len(matched_sources) != 1:
            raise MemoryNudgeError("nudge candidate evidence was not present in source")
        if any(contains_secret(value) or contains_injection(value) for value in (content, excerpt)):
            raise MemoryNudgeError("nudge candidate contained unsafe content")
        key = (kind, content)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(NudgeCandidate(kind, scope, content, excerpt, evidence_type))
    return candidates


def _candidate_key(candidate: NudgeCandidate) -> tuple[str, str, str, str, str]:
    return (
        candidate.kind,
        candidate.scope,
        candidate.content,
        candidate.evidence,
        candidate.evidence_type,
    )


def _existing_candidate_keys(memory_dir: Path) -> set[tuple[str, str, str, str, str]]:
    try:
        lines = _read_recent_lines(memory_dir)
    except OSError as exc:
        raise OSError("recent.jsonl could not be read") from exc

    existing: set[tuple[str, str, str, str, str]] = set()
    for line in lines:
        try:
            entry = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(entry, dict) or entry.get("origin") != "periodic_nudge":
            continue
        task = entry.get("task")
        summary = entry.get("summary")
        if not isinstance(task, str) or not task.startswith("[nudge] "):
            continue
        if not isinstance(summary, str):
            continue
        kind = entry.get("kind")
        scope = entry.get("scope")
        evidence_type = entry.get("evidence_type", entry.get("evidence"))
        if not all(isinstance(value, str) for value in (kind, scope, evidence_type)):
            continue
        existing.add((
            kind,
            scope,
            task.removeprefix("[nudge] "),
            summary,
            evidence_type,
        ))
    return existing


def _append_candidates(memory_dir: Path, candidates: list[NudgeCandidate]) -> None:
    """Append only missing candidates so an interrupted retry is idempotent."""
    recent = safe_file_in_dir(memory_dir, memory_dir / "recent.jsonl")
    if recent is None:
        raise OSError("recent.jsonl is unavailable or unsafe")
    existing = _existing_candidate_keys(memory_dir)
    now = datetime.now(timezone.utc).isoformat()
    with recent.open("a", encoding="utf-8") as output:
        for candidate in candidates:
            if _candidate_key(candidate) in existing:
                continue
            output.write(json.dumps({
                "task": f"[nudge] {candidate.content}",
                "summary": candidate.evidence,
                "timestamp": now,
                "kind": candidate.kind,
                "scope": candidate.scope,
                "evidence": candidate.evidence_type,
                "evidence_type": candidate.evidence_type,
                "origin": "periodic_nudge",
            }, ensure_ascii=False) + "\n")


async def run_nudge(
    memory_dir: Path,
    llm: "LLMPort",
    *,
    reviewer: "LLMPort | None" = None,
) -> NudgeReport:
    """Review one new work-evidence batch and append only safe candidates."""
    policy = load_memory_policy()
    evidence = _load_evidence(memory_dir)
    if evidence.error:
        append_memory_history(
            memory_dir,
            target="nudge",
            policy_version=policy.version,
            status="failed",
            error=evidence.error,
        )
        return NudgeReport("failed", error=evidence.error)
    if not evidence.source.strip():
        try:
            _write_offset(memory_dir, evidence.end_offset)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            append_memory_history(
                memory_dir,
                target="nudge",
                policy_version=policy.version,
                status="failed",
                error=error,
            )
            logger.warning("periodic memory nudge could not advance its offset", exc_info=True)
            return NudgeReport("failed", error=error)
        append_memory_history(
            memory_dir,
            target="nudge",
            policy_version=policy.version,
            status="unchanged",
        )
        return NudgeReport("unchanged")
    if reviewer is None:
        error = "periodic nudge requires a reviewer"
        append_memory_history(
            memory_dir,
            target="nudge",
            policy_version=policy.version,
            status="failed",
            old_text=evidence.source,
            error=error,
        )
        return NudgeReport("failed", error=error)

    try:
        outcome = await _generate_and_review_result(
            llm,
            reviewer,
            _NUDGE_PROMPT.format(
                max_candidates=_MAX_CANDIDATES,
                source=evidence.source,
            ),
            evidence.source,
            system_prompt=_NUDGE_SYSTEM_PROMPT,
            target_view="Periodic Memory Nudge",
            review_policy=_NUDGE_REVIEW_POLICY,
        )
        candidates = _parse_candidates(outcome.text, evidence)
    except (MemoryCompilationError, MemoryNudgeError) as exc:
        append_memory_history(
            memory_dir,
            target="nudge",
            policy_version=policy.version,
            status="rejected",
            old_text=evidence.source,
            review_rounds=getattr(exc, "review_rounds", 0),
            error=f"{type(exc).__name__}: {exc}",
        )
        return NudgeReport(
            "rejected",
            review_rounds=getattr(exc, "review_rounds", 0),
            error=str(exc),
        )
    except Exception as exc:
        append_memory_history(
            memory_dir,
            target="nudge",
            policy_version=policy.version,
            status="failed",
            old_text=evidence.source,
            error=f"{type(exc).__name__}: {exc}",
        )
        logger.warning("periodic memory nudge failed", exc_info=True)
        return NudgeReport("failed", error=f"{type(exc).__name__}: {exc}")

    try:
        # The candidate log is committed before its checkpoint advances.  If
        # appending fails, the source remains due; if a process stops after a
        # partial append, the next attempt de-duplicates exact candidates.
        if candidates:
            _append_candidates(memory_dir, candidates)
        _write_offset(memory_dir, evidence.end_offset)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        append_memory_history(
            memory_dir,
            target="nudge",
            policy_version=policy.version,
            status="failed",
            old_text=evidence.source,
            review_rounds=outcome.rounds,
            error=error,
        )
        logger.warning("periodic memory nudge could not persist its outcome", exc_info=True)
        return NudgeReport("failed", review_rounds=outcome.rounds, error=error)

    if not candidates:
        append_memory_history(
            memory_dir,
            target="nudge",
            policy_version=policy.version,
            status="unchanged",
            old_text=evidence.source,
            review_rounds=outcome.rounds,
        )
        return NudgeReport("unchanged", review_rounds=outcome.rounds)

    rendered = "\n".join(candidate.content for candidate in candidates)
    append_memory_history(
        memory_dir,
        target="nudge",
        policy_version=policy.version,
        status="written",
        old_text=evidence.source,
        new_text=rendered,
        review_rounds=outcome.rounds,
    )
    return NudgeReport("written", len(candidates), outcome.rounds)
