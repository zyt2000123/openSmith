"""Parse and apply a compiler-proposed change set to a rendered memory view.

The compiler used to emit a whole replacement document, which left two problems
with no cheap fix.  Nothing said *what* it had changed, so a bullet the model
simply forgot to copy was a silent deletion; and one unusable line failed the
entire draft, discarding every good line beside it.

A change set states each edit explicitly, so an untouched bullet is untouched by
construction, and a single bad edit can be rejected on its own while the rest
land.  Edits address bullets by their ``**{topic}**`` prefix -- guaranteed by the
view templates in policy sections 4 and 5 -- rather than by line number or exact
text, because a model that misplaces one character would otherwise fail to apply
an edit that is semantically fine.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

MemoryOp = Literal["add", "remove", "replace"]

_OPS: frozenset[str] = frozenset(("add", "remove", "replace"))

# `- **topic** — …` / `- **topic**: …`.  Both separators appear across the two
# view templates, so the key is captured before either.
_TOPIC_PATTERN = re.compile(r"^\s*-\s*\*\*(.+?)\*\*")

# Least valuable first, mirroring policy 5.1 for durable and 4 for context:
# transient status goes before verified conclusions, and the entries worth
# keeping longest are evicted last.
EVICTION_ORDER: dict[str, tuple[str, ...]] = {
    "durable": (
        "Active Work",
        "Pending",
        "Verified Outcomes",
        "Decisions",
        "Known Pitfalls",
    ),
    "context": (
        "Stable User Context",
        "Collaboration Patterns",
        "Confirmed Preferences",
    ),
}


@dataclass(frozen=True)
class MemoryChange:
    """One proposed edit, addressed by section plus topic key."""

    op: MemoryOp
    view: str
    section: str
    content: str = ""
    target: str = ""
    reason: str = ""
    evidence_ref: str = ""
    evidence_quote: str = ""

    @property
    def topic(self) -> str:
        """The bullet this change addresses: ``target`` for edits, else the new key."""
        if self.target:
            return self.target
        return topic_key(self.content) or ""


@dataclass(frozen=True)
class RejectedChange:
    """A change that will not be applied, and the machine-checkable reason."""

    change: MemoryChange | None
    reason: str
    detail: str = ""

    def describe(self) -> str:
        where = ""
        if self.change is not None:
            where = f"[{self.change.op} {self.change.section}/{self.change.topic}] "
        suffix = f" ({self.detail})" if self.detail else ""
        return f"{where}{self.reason}{suffix}"


def topic_key(bullet: str) -> str | None:
    """Return the ``**topic**`` key of a bullet, or None when it has none."""
    match = _TOPIC_PATTERN.match(bullet)
    if match is None:
        return None
    key = match.group(1).strip()
    return key or None


def parse_changeset(
    payload: object,
    *,
    view: str,
    sections: tuple[str, ...],
) -> tuple[list[MemoryChange], list[RejectedChange], bool]:
    """Split a decoded model payload into usable changes and structural rejects.

    The third return value is whether the model explicitly reported that nothing
    was worth recording.  That is a *success* with no edits, not a failure: a
    quiet stretch of routine work genuinely has nothing to remember, and counting
    it as failure would make an honest empty answer look like a broken pipeline.
    """
    if not isinstance(payload, dict):
        return [], [RejectedChange(None, "changeset_not_an_object")], False

    nothing_to_record = bool(payload.get("nothing_to_record"))
    raw_changes = payload.get("changes")
    if raw_changes is None:
        raw_changes = []
    if not isinstance(raw_changes, list):
        return [], [RejectedChange(None, "changes_not_a_list")], nothing_to_record

    changes: list[MemoryChange] = []
    rejected: list[RejectedChange] = []
    for raw in raw_changes:
        parsed, reject = _parse_one(raw, view=view, sections=sections)
        if parsed is not None:
            changes.append(parsed)
        elif reject is not None:
            rejected.append(reject)
    return changes, rejected, nothing_to_record


def _parse_one(
    raw: object,
    *,
    view: str,
    sections: tuple[str, ...],
) -> tuple[MemoryChange | None, RejectedChange | None]:
    if not isinstance(raw, dict):
        return None, RejectedChange(None, "change_not_an_object")

    op = str(raw.get("op") or "").strip().lower()
    if op not in _OPS:
        return None, RejectedChange(None, "unknown_op", str(raw.get("op"))[:40])

    raw_view = str(raw.get("view") or view).strip()
    if raw_view != view:
        # One compilation targets one view; a change naming another view would
        # otherwise be applied to the wrong document.
        return None, RejectedChange(None, "wrong_view", raw_view[:40])

    section = str(raw.get("section") or "").strip()
    if section not in sections:
        return None, RejectedChange(None, "unknown_section", section[:60])

    evidence = raw.get("evidence")
    ref = quote = ""
    if isinstance(evidence, dict):
        ref = str(evidence.get("ref") or "").strip()
        quote = str(evidence.get("quote") or "").strip()

    change = MemoryChange(
        op=op,  # type: ignore[arg-type]
        view=view,
        section=section,
        content=str(raw.get("content") or "").strip(),
        target=str(raw.get("target") or "").strip().strip("*").strip(),
        reason=str(raw.get("reason") or "").strip(),
        evidence_ref=ref,
        evidence_quote=quote,
    )

    if op in {"add", "replace"} and not change.content:
        return None, RejectedChange(change, "empty_content")
    if op in {"remove", "replace"} and not change.target:
        return None, RejectedChange(change, "missing_target")
    if op in {"add", "replace"} and topic_key(change.content) is None:
        # Without a key the bullet cannot be addressed by any later change, and
        # the applier would have no way to find it again.
        return None, RejectedChange(change, "content_has_no_topic_key")
    return change, None


def render_changeset(
    changes: list[MemoryChange],
    *,
    nothing_to_record: bool = False,
) -> str:
    """Re-emit changes in the wire contract, for review.

    The reviewer must be shown what will actually be written, not what the
    generator proposed.  Handing it a change the guards already refused invites a
    hard fail on content that was never going to land -- which would sink the
    surviving changes beside it and undo the whole point of per-change rejection.
    """
    payload = {
        "nothing_to_record": nothing_to_record,
        "changes": [
            {
                key: value
                for key, value in (
                    ("op", change.op),
                    ("view", change.view),
                    ("section", change.section),
                    ("content", change.content),
                    ("target", change.target),
                    ("reason", change.reason),
                    (
                        "evidence",
                        {"ref": change.evidence_ref, "quote": change.evidence_quote},
                    ),
                )
                if value
            }
            for change in changes
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def parse_document(text: str, sections: tuple[str, ...]) -> dict[str, list[str]]:
    """Group a rendered view's bullets by section heading.

    Content under an unknown heading is dropped, matching what the view
    validator already enforces: only the template's sections are legal.
    """
    grouped: dict[str, list[str]] = {section: [] for section in sections}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
            current = heading if heading in grouped else None
            continue
        if current is None:
            continue
        stripped = line.lstrip()
        if stripped.startswith("-"):
            body = stripped[1:].strip()
            if body:
                grouped[current].append(f"- {body}")
    return grouped


def render_document(
    title: str,
    sections: tuple[str, ...],
    grouped: dict[str, list[str]],
) -> str:
    parts = [f"# {title}"]
    for section in sections:
        parts.extend(["", f"## {section}", *grouped.get(section, [])])
    parts.append("")
    return "\n".join(parts)


def _find_indices(bullets: list[str], topic: str) -> list[int]:
    return [i for i, bullet in enumerate(bullets) if topic_key(bullet) == topic]


def apply_changes(
    grouped: dict[str, list[str]],
    changes: list[MemoryChange],
) -> tuple[dict[str, list[str]], list[MemoryChange], list[RejectedChange]]:
    """Apply changes in order; return the new grouping plus what landed and what did not.

    A change that cannot be located is rejected on its own.  Nothing else in the
    document is touched, so bullets no change names survive untouched -- the
    property that makes "forgot to copy it" impossible.
    """
    result = {section: list(bullets) for section, bullets in grouped.items()}
    applied: list[MemoryChange] = []
    rejected: list[RejectedChange] = []

    for change in changes:
        bullets = result.setdefault(change.section, [])
        if change.op == "add":
            if _find_indices(bullets, change.topic):
                rejected.append(RejectedChange(change, "topic_already_exists"))
                continue
            bullets.append(change.content)
            applied.append(change)
            continue

        matches = _find_indices(bullets, change.target)
        if not matches:
            rejected.append(RejectedChange(change, "target_not_found"))
            continue
        if len(matches) > 1:
            # The template forbids duplicate topics inside one section, but a
            # model can still emit them.  Guessing which one was meant risks
            # editing the wrong memory, so refuse instead.
            rejected.append(
                RejectedChange(change, "target_is_ambiguous", f"{len(matches)} matches")
            )
            continue

        index = matches[0]
        if change.op == "remove":
            bullets.pop(index)
            applied.append(change)
            continue

        # replace
        new_key = topic_key(change.content)
        if new_key != change.target:
            # Renaming a topic in place would leave the old key unreachable to
            # every later change; express it as remove + add instead.
            rejected.append(
                RejectedChange(change, "replace_changes_topic_key", str(new_key)[:60])
            )
            continue
        bullets[index] = change.content
        applied.append(change)

    return result, applied, rejected


def evict_to_budget(
    grouped: dict[str, list[str]],
    *,
    title: str,
    sections: tuple[str, ...],
    order: tuple[str, ...],
    max_chars: int,
) -> tuple[str, list[str]]:
    """Render within budget by dropping whole bullets in policy order.

    Whole bullets only: half a fact reads as a fact.  Returns the document and
    the bullets that were evicted so the caller can record them -- a memory that
    vanishes without a trace is indistinguishable from one that was never made.
    """
    working = {section: list(bullets) for section, bullets in grouped.items()}
    evicted: list[str] = []
    document = render_document(title, sections, working)
    for section in order:
        while len(document) > max_chars and working.get(section):
            evicted.append(working[section].pop(0))  # oldest bullet in this section
            document = render_document(title, sections, working)
    return document, evicted
