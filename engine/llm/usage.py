"""Best-effort normalization of provider usage payloads.

Providers and gateways report token usage in different dialects; a single
response may even mix dialects (an OpenAI-compatible gateway proxying
DeepSeek returns both ``prompt_tokens_details.cached_tokens`` and
``prompt_cache_hit_tokens``).  Normalization therefore matches known field
names in priority order instead of branching per provider.

Detail fields are strictly best-effort: absent means zero, never estimated.
"""

from __future__ import annotations

from typing import Any

USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)
# Not a token count — 1 when the provider sent a usage payload, 0 when it did not.
# Kept out of USAGE_KEYS so anything iterating token keys stays unaffected.
USAGE_REPORTED_KEY = "usage_reported"


def _number(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0:
        return None
    return int(value)


def _first(raw: dict[str, Any], *paths: tuple[str, ...]) -> int:
    """Return the first present non-negative number among dotted key paths."""
    for path in paths:
        value: Any = raw
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        number = _number(value)
        if number is not None:
            return number
    return 0


def normalize_usage(raw: object) -> dict[str, int]:
    """Normalize one provider usage payload into the six internal token keys.

    Also records whether the provider sent a usage payload at all: relays that
    omit ``usage`` made real, billed calls indistinguishable from genuine zeros,
    silently skewing cost and cache-hit statistics.  Aggregated across runs the
    flag reads naturally as "how many calls reported usage" — no consumer sums
    this dict blindly, they all read named token keys.
    """
    if not isinstance(raw, dict):
        return dict.fromkeys(USAGE_KEYS, 0) | {USAGE_REPORTED_KEY: 0}

    input_tokens = _first(raw, ("prompt_tokens",), ("input_tokens",))
    output_tokens = _first(raw, ("completion_tokens",), ("output_tokens",))
    total_tokens = _first(raw, ("total_tokens",)) or input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_read_tokens": _first(
            raw,
            ("prompt_tokens_details", "cached_tokens"),
            ("prompt_cache_hit_tokens",),
            ("cache_read_input_tokens",),
        ),
        "cache_write_tokens": _first(raw, ("cache_creation_input_tokens",)),
        "reasoning_tokens": _first(
            raw,
            ("completion_tokens_details", "reasoning_tokens"),
        ),
        USAGE_REPORTED_KEY: 1,
    }
