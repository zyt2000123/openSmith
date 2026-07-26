from __future__ import annotations

from typing import Literal

from engine.tool.schema import function_to_schema


def test_function_to_schema_supports_pep604_optional_and_dict_types():
    def sample(
        query: str,
        optional_text: str | None,
        tags: list[str],
        scores: dict[str, int],
    ) -> None:
        """Sample tool."""

    schema = function_to_schema(sample)["function"]["parameters"]

    assert schema["properties"]["optional_text"] == {"type": "string"}
    assert schema["properties"]["tags"] == {"type": "array", "items": {"type": "string"}}
    assert schema["properties"]["scores"] == {
        "type": "object",
        "additionalProperties": {"type": "integer"},
    }
    assert schema["required"] == ["query", "tags", "scores"]


def test_function_to_schema_supports_literal_unions():
    def sample(action: Literal["add", "remove"], value: int | float) -> None:
        pass

    schema = function_to_schema(sample)["function"]["parameters"]

    assert schema["properties"]["action"] == {
        "enum": ["add", "remove"],
        "type": "string",
    }
    assert schema["properties"]["value"] == {
        "anyOf": [{"type": "integer"}, {"type": "number"}],
    }


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    raise SystemExit(1 if failures else 0)
