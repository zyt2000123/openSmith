"""``ScopedToolRegistry.get_schemas(enabled=…)`` — the by-name narrowing path.

This is how the ReAct loop's lazy-schema loader resolves one tool contract at
a time. It crashed for every scoped view: ``_active_names`` returned a
frozenset and ``get_schemas`` called ``intersection_update`` on it.
"""

from __future__ import annotations

import pytest

from engine.tool.registry import ScopedToolRegistry, ToolRegistry


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    for name in ("alpha", "beta", "gamma"):
        registry.register(
            name,
            f"{name} tool",
            {"properties": {}},
            lambda: "ok",
            permission_level="read",
            approval_policy="never",
            side_effect="none",
        )
    return registry


def test_narrowing_a_scoped_view_by_name_does_not_crash() -> None:
    scoped = ScopedToolRegistry(_registry(), ["alpha", "beta"])
    schemas = scoped.get_schemas(enabled=["alpha"])
    assert [s["function"]["name"] for s in schemas] == ["alpha"]


def test_narrowing_cannot_widen_past_the_scope() -> None:
    scoped = ScopedToolRegistry(_registry(), ["alpha"])
    assert scoped.get_schemas(enabled=["gamma"]) == []
    assert scoped.get_schemas(enabled=["alpha", "gamma"]) != []
    assert {s["function"]["name"] for s in scoped.get_schemas(enabled=["alpha", "gamma"])} == {"alpha"}


def test_narrowing_cannot_widen_past_the_parents_enabled_set() -> None:
    registry = _registry()
    registry.set_enabled(["alpha"])
    scoped = ScopedToolRegistry(registry, ["alpha", "beta"])
    assert {s["function"]["name"] for s in scoped.get_schemas()} == {"alpha"}
    assert scoped.get_schemas(enabled=["beta"]) == []


@pytest.mark.parametrize("names", [[], ["alpha"], ["alpha", "beta", "gamma"]])
def test_repeated_calls_do_not_mutate_the_scope(names: list[str]) -> None:
    scoped = ScopedToolRegistry(_registry(), ["alpha", "beta"])
    scoped.get_schemas(enabled=names)
    # A mutated frozenset would have narrowed the view permanently.
    assert {s["function"]["name"] for s in scoped.get_schemas()} == {"alpha", "beta"}
