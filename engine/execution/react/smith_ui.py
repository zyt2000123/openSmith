"""Validation for the declarative terminal UI event contract.

The execution engine never forwards an arbitrary model-produced object as UI.
Only the ``render_ui`` presentation tool may produce this payload, after this
bounded structural and attachment-path validation. The Ink renderer performs a
second, component-props schema validation before it renders anything.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_ALLOWED_COMPONENTS = frozenset(
    {
        "Box",
        "Text",
        "Newline",
        "Spacer",
        "Heading",
        "Divider",
        "Badge",
        "ProgressBar",
        "Sparkline",
        "BarChart",
        "Table",
        "List",
        "ListItem",
        "Card",
        "KeyValue",
        "StatusLine",
        "Metric",
        "Callout",
    }
)
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
_ELEMENT_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_MAX_ELEMENTS = 64
_MAX_DEPTH = 8
_MAX_IMAGES = 4
_MAX_STRING_LENGTH = 12_000
_MAX_FALLBACK_STRING_LENGTH = 2_000
_MAX_FALLBACK_CODE_LENGTH = 16_000


@dataclass(frozen=True)
class SmithUiValidation:
    ok: bool
    payload: dict[str, Any] | None = None
    reason: str = ""


def _reject(reason: str) -> SmithUiValidation:
    return SmithUiValidation(False, reason=reason)


def _fallback_value(value: Any, depth: int = 0) -> Any:
    """Bound untrusted tool arguments before placing them in a code fallback."""
    if depth > _MAX_DEPTH:
        return "[truncated: nesting limit]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "[non-finite number]"
    if isinstance(value, str):
        return value[:_MAX_FALLBACK_STRING_LENGTH]
    if isinstance(value, list):
        return [_fallback_value(item, depth + 1) for item in value[:_MAX_ELEMENTS]]
    if isinstance(value, dict):
        return {
            str(key)[:128]: _fallback_value(item, depth + 1)
            for key, item in list(value.items())[:_MAX_ELEMENTS]
        }
    return f"[{type(value).__name__}]"


def smith_ui_fallback(arguments: Any, reason: str) -> dict[str, str]:
    """Create bounded JSON source for the terminal's CodeBlock fallback."""
    code = json.dumps(_fallback_value(arguments), ensure_ascii=False, indent=2, sort_keys=True)
    return {"reason": reason, "code": code[:_MAX_FALLBACK_CODE_LENGTH]}


def _is_json_value(value: Any, depth: int = 0) -> bool:
    if depth > _MAX_DEPTH:
        return False
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, str):
        return len(value) <= _MAX_STRING_LENGTH
    if isinstance(value, (int, float)):
        return not isinstance(value, bool) and math.isfinite(value)
    if isinstance(value, list):
        return len(value) <= _MAX_ELEMENTS and all(_is_json_value(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return (
            len(value) <= _MAX_ELEMENTS
            and all(
                isinstance(key, str)
                and not key.startswith("$")
                and _is_json_value(item, depth + 1)
                for key, item in value.items()
            )
        )
    return False


def _validate_spec(raw_spec: Any) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(raw_spec, dict) or set(raw_spec) != {"root", "elements"}:
        return None, "smith-ui spec must contain only root and elements"
    root = raw_spec.get("root")
    elements = raw_spec.get("elements")
    if not isinstance(root, str) or not _ELEMENT_ID.fullmatch(root):
        return None, "smith-ui root must be a valid element id"
    if not isinstance(elements, dict) or not (1 <= len(elements) <= _MAX_ELEMENTS):
        return None, "smith-ui must contain between 1 and 64 elements"
    if root not in elements:
        return None, "smith-ui root must reference an existing element"

    normalized: dict[str, dict[str, Any]] = {}
    for element_id, raw_element in elements.items():
        if not isinstance(element_id, str) or not _ELEMENT_ID.fullmatch(element_id):
            return None, "smith-ui element ids must be simple identifiers"
        if not isinstance(raw_element, dict) or set(raw_element) != {"type", "props", "children"}:
            return None, "smith-ui elements must contain only type, props, and children"
        component_type = raw_element.get("type")
        props = raw_element.get("props")
        children = raw_element.get("children")
        if component_type not in _ALLOWED_COMPONENTS:
            return None, f"smith-ui component {component_type!r} is not permitted"
        if not isinstance(props, dict) or not _is_json_value(props):
            return None, "smith-ui props must be bounded JSON without dynamic expressions"
        if not isinstance(children, list) or len(children) > _MAX_ELEMENTS:
            return None, "smith-ui children must be a bounded list"
        if any(not isinstance(child, str) or not _ELEMENT_ID.fullmatch(child) for child in children):
            return None, "smith-ui children must be valid element ids"
        if len(set(children)) != len(children):
            return None, "smith-ui children must not repeat an element"
        normalized[element_id] = {"type": component_type, "props": props, "children": list(children)}

    if any(child not in normalized for element in normalized.values() for child in element["children"]):
        return None, "smith-ui children must reference existing elements"

    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(element_id: str, depth: int) -> str | None:
        if depth > _MAX_DEPTH:
            return "smith-ui nesting exceeds the depth limit"
        if element_id in visiting:
            return "smith-ui must not contain cycles"
        if element_id in visited:
            return None
        visiting.add(element_id)
        for child in normalized[element_id]["children"]:
            error = visit(child, depth + 1)
            if error:
                return error
        visiting.remove(element_id)
        visited.add(element_id)
        return None

    error = visit(root, 0)
    if error:
        return None, error
    if len(visited) != len(normalized):
        return None, "smith-ui must not contain orphaned elements"
    return {"root": root, "elements": normalized}, ""


def _validate_images(raw_images: Any, working_dir: Path | None) -> tuple[list[dict[str, Any]] | None, str]:
    if raw_images is None:
        return [], ""
    if not isinstance(raw_images, list) or len(raw_images) > _MAX_IMAGES:
        return None, "smith-ui images must be a list of at most four attachments"
    if raw_images and working_dir is None:
        return None, "smith-ui images require a local working directory"

    root = working_dir.resolve() if working_dir is not None else None
    images: list[dict[str, Any]] = []
    for raw_image in raw_images:
        if not isinstance(raw_image, dict) or not set(raw_image).issubset({"path", "alt", "width", "height"}):
            return None, "smith-ui image attachments contain unsupported fields"
        path = raw_image.get("path")
        alt = raw_image.get("alt")
        if not isinstance(path, str) or not path or "://" in path or path.startswith("file:"):
            return None, "smith-ui images must use local file paths"
        if not isinstance(alt, str) or not alt or len(alt) > 500:
            return None, "smith-ui image attachments require bounded alt text"
        assert root is not None
        try:
            candidate = Path(path).expanduser()
            resolved = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        except (OSError, ValueError):
            return None, "smith-ui image path is invalid"
        if not resolved.is_relative_to(root):
            return None, "smith-ui image must stay inside the working directory"
        if not resolved.is_file() or resolved.suffix.lower() not in _IMAGE_EXTENSIONS:
            return None, "smith-ui image must be an existing supported image file"
        image: dict[str, Any] = {"path": str(resolved), "alt": alt}
        for dimension in ("width", "height"):
            value = raw_image.get(dimension)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 120:
                return None, f"smith-ui image {dimension} must be an integer between 1 and 120"
            image[dimension] = value
        images.append(image)
    return images, ""


def validate_smith_ui_call(arguments: Any, *, working_dir: Path | None) -> SmithUiValidation:
    """Return a normalized, safe UI payload or an explanatory rejection."""
    if not isinstance(arguments, dict) or not set(arguments).issubset({"spec", "images"}) or "spec" not in arguments:
        return _reject("render_ui accepts only spec and optional images")
    spec, spec_error = _validate_spec(arguments.get("spec"))
    if spec is None:
        return _reject(spec_error)
    images, image_error = _validate_images(arguments.get("images"), working_dir)
    if images is None:
        return _reject(image_error)
    return SmithUiValidation(True, {"version": 1, "spec": spec, "images": images})
