"""Deterministic adjudication of a proposed change set (policy section 6.1).

Three questions a model must not be trusted to answer about its own output, and
that code can decide without a second opinion:

1. *Traceability* -- does the cited evidence exist, and does the new content stay
   inside it?
2. *Retention* -- may this bullet be deleted at all?
3. *Placement* -- is this section allowed to hold a claim of this strength?

These rules used to bind only the deterministic fallback renderer, which meant
the code-written path was held to a stricter standard than the LLM-written one.
They now run on every change, before the reviewer, so a change set that fails
them costs no reviewer call and comes back to the generator with a
machine-checkable reason instead of prose.

The guards deliberately only judge what has a falsifiable anchor.  "This user
prefers terse answers" cannot be checked by any amount of code; policy section 7
hands that class to the reviewer.
"""

from __future__ import annotations

import re

from ._changeset import MemoryChange, RejectedChange, topic_key

# Bullets here are conclusions -- verified results, settled decisions, confirmed
# preferences.  Ordinary progress evidence does not invalidate a conclusion, so
# erasing one takes an explicit forget or correction.
_CONCLUSION_SECTIONS: frozenset[str] = frozenset((
    "Verified Outcomes",
    "Decisions",
    "Known Pitfalls",
    "Confirmed Preferences",
    "Collaboration Patterns",
    "Stable User Context",
))

_FORGET_KINDS: frozenset[str] = frozenset(("forget", "correction"))

# An automatic ``work`` event's summary is the assistant's own account of what it
# did, not a tool or test result; ``partial_work`` says so in its name.  Neither
# can establish a *verified* outcome.
_UNVERIFIED_KINDS: frozenset[str] = frozenset(("work", "partial_work"))

# Evidence renders one entry per line as ``- [<timestamp>] (meta) task: summary``
# (see compile._entries_to_source).  A summary containing a newline spills onto
# following lines, so anything that does not open a new entry belongs to the
# previous one.
_ENTRY_START = re.compile(r"^- \[([^\]]*)\]")
_KIND = re.compile(r"\bkind=([\w-]+)")

# Content fragments that are either present in the evidence or invented: a model
# cannot half-remember a line number or a URL.  Prose is not checkable this way
# and is left to the reviewer.
#
# The leading \b on the path pattern is load-bearing, not decoration.  CJK
# characters are word characters, so without it `修复了loader.py` matches as one
# anchor and then fails to be found in evidence that says `loader.py` -- a
# spurious rejection.  With it, a path glued to Chinese text simply goes
# unchecked, which errs towards permitting rather than towards refusing a change
# that was fine.
_ANCHOR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"https?://\S+"),
    re.compile(r"`([^`\n]+)`"),
    re.compile(
        r"\b[\w./-]*[\w-]+\."
        r"(?:py|pyi|ts|tsx|js|jsx|md|json|ya?ml|toml|sh|sql|txt|html|css|rs|go"
        r"|java|kt|swift|c|cc|cpp|h|hpp)(?::\d+)?\b"
    ),
    re.compile(r"\d{6,}"),
)

# The Verified Outcomes template carries an explicit evidence field; a verified
# result with no stated evidence is exactly the unverified "已修复" claim that
# policy 5.1 forbids.  Both spellings are accepted because the template is
# Chinese but nothing stops a user's project memory from being written in English.
_EVIDENCE_FIELD_MARKERS: tuple[str, ...] = ("证据", "evidence")


def build_evidence_index(source: str) -> dict[str, list[str]]:
    """Index the rendered evidence block by ``ref`` (the bracketed timestamp).

    Built from the prompt text rather than from ``recent.jsonl`` on purpose: the
    source is capped, so the log holds entries the model never saw, and a ref
    validated against the log could be accepted without ever having been read.
    """
    index: dict[str, list[str]] = {}
    current: str | None = None
    for line in source.splitlines():
        match = _ENTRY_START.match(line)
        if match:
            current = match.group(1).strip()
            index.setdefault(current, []).append(line)
            continue
        if current is not None and index.get(current):
            index[current][-1] = f"{index[current][-1]}\n{line}"
    return index


def adjudicate(
    changes: list[MemoryChange],
    *,
    view: str,
    evidence: dict[str, list[str]],
    grouped: dict[str, list[str]],
) -> tuple[list[MemoryChange], list[RejectedChange]]:
    """Split proposed changes into those code will allow and those it refuses.

    Rejections are per change, never per change set: a single unsupported edit
    must not discard the supported ones beside it.
    """
    accepted_bullets = _index_by_topic(grouped)
    allowed: list[MemoryChange] = []
    refused: list[RejectedChange] = []
    for change in changes:
        cited = _cited_entries(change, evidence)
        reject = (
            _check_traceable(change, evidence, cited, accepted_bullets)
            or _check_retention(change, cited)
            or _check_placement(change, cited, view)
        )
        if reject is None:
            allowed.append(change)
        else:
            refused.append(reject)
    return allowed, refused


def _normalize(text: str) -> str:
    """Collapse whitespace so a re-wrapped quote still matches its source."""
    return " ".join(text.split())


def _anchors(text: str) -> list[str]:
    found: list[str] = []
    for pattern in _ANCHOR_PATTERNS:
        for match in pattern.finditer(text):
            value = (match.group(1) if match.groups() else match.group(0)).strip()
            if value:
                found.append(value)
    return found


def _kind_of(entry: str) -> str:
    """The event's ``kind``, defaulting as ``_entries_for_view`` does.

    A pre-``kind`` event renders no ``kind=`` at all.  Treating it as ``work``
    matches the selection logic and fails safe: ``work`` is the weakest kind, so
    an unlabelled event can never establish a verified outcome.
    """
    match = _KIND.search(entry)
    return match.group(1) if match else "work"


def _index_by_topic(grouped: dict[str, list[str]]) -> dict[tuple[str, str], str]:
    index: dict[tuple[str, str], str] = {}
    for section, bullets in grouped.items():
        for bullet in bullets:
            key = topic_key(bullet)
            if key:
                index.setdefault((section, key), bullet)
    return index


def _cited_entries(change: MemoryChange, evidence: dict[str, list[str]]) -> list[str]:
    """The indexed entries that both carry the ref and contain the quote."""
    candidates = evidence.get(change.evidence_ref, [])
    quote = _normalize(change.evidence_quote)
    if not quote:
        return []
    return [entry for entry in candidates if quote in _normalize(entry)]


def _kinds(cited: list[str]) -> set[str]:
    return {_kind_of(entry) for entry in cited}


def _check_traceable(
    change: MemoryChange,
    evidence: dict[str, list[str]],
    cited: list[str],
    accepted: dict[tuple[str, str], str],
) -> RejectedChange | None:
    """Guard 1: the citation must resolve, and the content must stay inside it."""
    if not change.evidence_ref or not change.evidence_quote:
        return RejectedChange(change, "evidence_missing")
    if change.evidence_ref not in evidence:
        return RejectedChange(
            change, "evidence_ref_not_in_batch", change.evidence_ref[:40]
        )
    if not cited:
        return RejectedChange(
            change, "evidence_quote_not_in_event", change.evidence_quote[:80]
        )
    if not change.content:
        return None

    pool = [_normalize(entry) for entry in cited]
    if change.op == "replace":
        # A replace rewrites one bullet in place, so anchors the accepted bullet
        # already carried have been through review once; only what this change
        # *adds* has to come from the new batch.
        previous = accepted.get((change.section, change.target))
        if previous:
            pool.append(_normalize(previous))

    for anchor in _anchors(change.content):
        needle = _normalize(anchor)
        if not any(needle in text for text in pool):
            return RejectedChange(change, "content_anchor_not_in_evidence", anchor[:80])
    return None


def _check_retention(
    change: MemoryChange,
    cited: list[str],
) -> RejectedChange | None:
    """Guard 2: a bullet may only leave the document for a stated reason."""
    if change.op == "add":
        return None
    kinds = _kinds(cited)
    if change.op == "remove" and change.section in _CONCLUSION_SECTIONS:
        # A settled conclusion is not undone by more work happening; it takes the
        # user forgetting it or correcting it.
        if not kinds & _FORGET_KINDS:
            return RejectedChange(
                change,
                "conclusion_removed_without_forget_or_correction",
                ",".join(sorted(kinds))[:60],
            )
        return None
    if not change.reason and not kinds & _FORGET_KINDS:
        return RejectedChange(change, "deletion_without_reason")
    return None


def _check_placement(
    change: MemoryChange,
    cited: list[str],
    view: str,
) -> RejectedChange | None:
    """Guard 3: a section may not hold a claim stronger than its evidence.

    Durable only -- the context sections carry no strength ordering, so there is
    nothing here to decide for them.
    """
    if view != "durable" or change.op == "remove":
        return None
    kinds = _kinds(cited)

    if "partial_work" in kinds and change.section != "Active Work":
        return RejectedChange(change, "partial_work_outside_active_work", change.section)

    if change.section != "Verified Outcomes":
        return None
    if kinds <= _UNVERIFIED_KINDS:
        return RejectedChange(
            change,
            "unverified_evidence_in_verified_outcomes",
            ",".join(sorted(kinds))[:60],
        )
    body = change.content.split("**", 2)[-1].lower()
    if not any(marker in body for marker in _EVIDENCE_FIELD_MARKERS):
        return RejectedChange(change, "verified_outcome_without_evidence_field")
    return None
