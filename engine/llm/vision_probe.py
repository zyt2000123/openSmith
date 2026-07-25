"""One small probe to learn whether a model accepts images.

Neither of the usual answers holds up: a config flag puts the burden on the
user and goes stale, and a model-name table misses every model released after
it was written. Sending one 1x1 pixel and reading the provider's own answer
costs a fraction of a cent and cannot be wrong about what that endpoint
accepts today.

The result is cached per model for the life of the process. ``UNKNOWN`` is
deliberately *not* cached: it means the probe itself did not complete (auth,
rate limit, transport), and caching that would let one blip disable images for
the rest of the session.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from engine.llm.observability import llm_purpose

logger = logging.getLogger(__name__)

# 64x64 PNG, 154 bytes: a black square on white.
#
# Deliberately not a 1x1 pixel. A relay answered that one with
# HTTP 400 "You uploaded an unsupported image" — a verdict on the *image*,
# which _classify cannot tell apart from a verdict on image support, so a
# vision-capable model came back "unsupported". 64x64 clears every minimum
# dimension in the wild while staying one image tile.
_PROBE_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAYUlEQVR42u3asQkAIAxFQb+4"
    "/8pxhhQi4r0+4EGagKmq8XJzPB4AAAAAAAAAAADAvVZ3IMnRB3XvEysEAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAA0C7+CwEAAAAAAAAAAAB8C9iYUQl7FJC2bQAAAABJRU5ErkJggg=="
)

_PROBE_PROMPT = "Reply with: ok"

# These say the probe never reached a verdict. Treating them as "unsupported"
# would turn a rate limit into a permanent feature loss.
_INCONCLUSIVE_MARKERS = ("http 401", "http 403", "http 408", "http 429", "http 5")


class VisionSupport(str, Enum):
    """What one probe established about a model's image support."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    # The probe did not complete — retry later rather than concluding anything.
    UNKNOWN = "unknown"


_CACHE: dict[str, VisionSupport] = {}


def reset_vision_cache() -> None:
    """Drop cached verdicts (tests, and config changes within one process)."""
    _CACHE.clear()


def _cache_key(llm: Any) -> str:
    return f"{getattr(llm, 'provider', '')}|{getattr(llm, 'model', '')}"


def _probe_messages() -> list[dict[str, Any]]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _PROBE_PROMPT},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{_PROBE_PNG}"},
                },
            ],
        }
    ]


def _classify(error: BaseException) -> VisionSupport:
    message = str(error).casefold()
    if any(marker in message for marker in _INCONCLUSIVE_MARKERS):
        return VisionSupport.UNKNOWN
    if "http 4" in message:
        # A 4xx that is not auth/rate-limit is the endpoint refusing this
        # request shape — which is exactly what the probe is asking about.
        return VisionSupport.UNSUPPORTED
    return VisionSupport.UNKNOWN


async def probe_vision(llm: Any) -> VisionSupport:
    """Send the pixel once and report what the provider said. No caching."""
    try:
        with llm_purpose("vision_probe"):
            await llm.chat(_probe_messages())
    except Exception as error:  # noqa: BLE001 — every failure has to be classified
        verdict = _classify(error)
        logger.debug(
            "vision probe for %s: %s (%s)",
            getattr(llm, "model", "?"),
            verdict.value,
            error,
        )
        return verdict
    return VisionSupport.SUPPORTED


async def vision_support(llm: Any) -> VisionSupport:
    """Cached ``probe_vision``: at most one probe per model per process."""
    key = _cache_key(llm)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    verdict = await probe_vision(llm)
    if verdict is not VisionSupport.UNKNOWN:
        _CACHE[key] = verdict
    return verdict
