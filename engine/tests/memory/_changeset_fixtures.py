"""Shared helpers for driving the change-set compiler contract from tests.

The compiler no longer returns a document, so a fixture cannot simply hand back
the Markdown it wants written.  These helpers turn a *desired* document into the
change set that would produce it, with an ``evidence.ref``/``quote`` pair taken
from the evidence the prompt actually showed -- inventing those would exercise
the reject path instead of the happy one.
"""

from __future__ import annotations

import json

EVIDENCE_MARKER = "copied verbatim from the same line:\n"


def selected_evidence(prompt: str) -> str:
    if EVIDENCE_MARKER not in prompt:
        return "- **Test evidence**: verified."
    evidence = (
        prompt.split(EVIDENCE_MARKER, 1)[1].split("\n\nLegal `section`", 1)[0].strip()
    )
    return evidence or "- **Test evidence**: verified."


def evidence_ref_and_quote(prompt: str) -> tuple[str, str]:
    """Extract a real (ref, quote) pair from the prompt's evidence block.

    Evidence renders as ``- [<timestamp>] (meta) task: summary``.
    """
    first = next(
        (line for line in selected_evidence(prompt).splitlines() if line.strip()), ""
    )
    ref = ""
    if first.startswith("- [") and "]" in first:
        ref = first[3 : first.index("]")]
    quote = first
    if "] " in quote:
        quote = quote.split("] ", 1)[1]
    if quote.startswith("(") and ") " in quote:
        quote = quote.split(") ", 1)[1]
    return ref, quote.strip()[:80]


def _bullets_by_section(document: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    section = ""
    for line in document.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        stripped = line.lstrip()
        if section and stripped.startswith("-"):
            body = stripped[1:].strip()
            if body:
                pairs.append((section, f"- {body}"))
    return pairs


def changeset_from_document(document: str, prompt: str) -> str:
    """Emit the change set whose application reproduces *document*'s bullets."""
    ref, quote = evidence_ref_and_quote(prompt)
    changes = [
        {
            "op": "add",
            "section": section,
            "content": bullet,
            "evidence": {"ref": ref, "quote": quote},
        }
        for section, bullet in _bullets_by_section(document)
    ]
    payload: dict[str, object] = {"changes": changes}
    if not changes:
        # An empty target document is the model saying there was nothing worth
        # recording, which the contract treats as success rather than failure.
        payload["nothing_to_record"] = True
    return json.dumps(payload, ensure_ascii=False)


def changeset_add(section: str, content: str, prompt: str, **extra: object) -> str:
    """A single-change set, for tests that only need one edit to land."""
    ref, quote = evidence_ref_and_quote(prompt)
    change: dict[str, object] = {
        "op": "add",
        "section": section,
        "content": content,
        "evidence": {"ref": ref, "quote": quote},
    }
    change.update(extra)
    return json.dumps({"changes": [change]}, ensure_ascii=False)
