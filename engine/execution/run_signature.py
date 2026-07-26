"""Stable, model-independent signatures for execution regression tests."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .events import EventType, ExecutionEvent


@dataclass(frozen=True)
class RunSignature:
    """The observable run shape worth comparing across harness changes."""

    events: tuple[str, ...]
    tools: tuple[str, ...]


def signature_of(events: Iterable[ExecutionEvent]) -> RunSignature:
    collected = list(events)
    return RunSignature(
        events=tuple(event.type.value for event in collected),
        tools=tuple(
            str(event.data.get("name", ""))
            for event in collected
            if event.type is EventType.TOOL_CALL_START
        ),
    )


def signature_diff(expected: RunSignature, actual: RunSignature) -> str:
    """Return a readable first divergence, or ``""`` when runs match."""
    if expected == actual:
        return ""
    parts: list[str] = []
    if expected.tools != actual.tools:
        parts.append(_first_divergence("tool", expected.tools, actual.tools))
    if expected.events != actual.events:
        parts.append(_first_divergence("event", expected.events, actual.events))
    return "\n".join(parts)


def _first_divergence(
    label: str,
    expected: Sequence[str],
    actual: Sequence[str],
) -> str:
    for index, (left, right) in enumerate(zip(expected, actual)):
        if left != right:
            return f"{label}[{index}]: expected {left!r}, got {right!r}"
    return f"{label} count: expected {len(expected)}, got {len(actual)}"


__all__ = ("RunSignature", "signature_diff", "signature_of")
