"""Turn image paths the user typed into messages the configured models can read.

In a terminal the natural way to hand over an image is to drag it into the
prompt, which pastes its path.  So "the user attached an image" means "the
user's text contains a path to a local image file", and this module resolves
those paths into conversation messages.

Three outcomes, all expressed as *messages to splice in ahead of the user
turn* so that callers need no branching:

* the main model can see images  -> a user turn carrying the image blocks
* it cannot, but an image model is configured -> a user turn carrying that
  model's transcription
* neither -> a system turn telling the model to explain the limitation, which
  lets it answer in the user's own language instead of a hardcoded string

Only text the *user* typed may reach this module.  A path chosen by the model
must go through the tool layer and its guards; reading arbitrary files because
they were mentioned in generated text would hand the model a filesystem
side channel.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine.llm.observability import llm_purpose
from engine.llm.vision_probe import VisionSupport, vision_support

logger = logging.getLogger(__name__)

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024

_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_EXTENSIONS = "png|jpe?g|gif|webp"

# Where an image path *ends* is unambiguous; where it begins is not. macOS
# hands out both "…/Screenshot\ 2026.png" (dragged into the terminal) and
# "…/Screen Shot 2026.png" (Copy as Pathname), and CJK text has no word
# separators at all, so "看看/Users/me/x.png" is one unbroken run.
#
# So the pattern claims a generous span that ends at an image extension and
# leaves the start to _resolve, which walks candidate beginnings and lets the
# filesystem settle it. Guessing here instead would need one alternative per
# quoting style and still lose to the ambiguous cases.
_CANDIDATE = re.compile(rf"[^\n]{{1,300}}?\.(?:{_EXTENSIONS})", re.IGNORECASE)

# Bounds the stat() calls one candidate span can trigger.
_MAX_STARTS = 16

_DESCRIBE_PROMPT = (
    "Describe this image for a colleague who cannot see it. Transcribe every piece "
    "of visible text verbatim — error messages, code, labels, numbers, file names — "
    "then describe the layout and anything else visually significant. Do not "
    "interpret, summarise, or give advice."
)


@dataclass(frozen=True)
class ImageAttachment:
    path: Path
    media_type: str
    data_base64: str

    @property
    def data_url(self) -> str:
        return f"data:{self.media_type};base64,{self.data_base64}"


def find_images(text: str, working_dir: Path | None = None) -> tuple[list[ImageAttachment], list[str]]:
    """Load every local image the text points at.

    Returns the attachments plus the names of files that looked like images but
    could not be used, so the caller can say so instead of silently dropping
    what the user handed over.
    """
    attachments: list[ImageAttachment] = []
    skipped: list[str] = []
    seen: set[Path] = set()

    for match in _CANDIDATE.finditer(text):
        path = _resolve(match.group(0), working_dir)
        if path is None or path in seen:
            continue
        seen.add(path)
        if len(attachments) >= MAX_IMAGES:
            skipped.append(path.name)
            continue
        attachment = _load(path)
        if attachment is None:
            skipped.append(path.name)
            continue
        attachments.append(attachment)

    return attachments, skipped


async def resolve_image_messages(
    text: str,
    working_dir: Path | None,
    llm: Any,
    vision_llm: Any | None = None,
) -> list[dict[str, Any]]:
    """Return messages to splice in ahead of the user turn (empty when no images)."""
    attachments, skipped = find_images(text, working_dir)
    if not attachments and not skipped:
        return []

    note = _skipped_note(skipped)
    if not attachments:
        return [{"role": "system", "content": note.strip()}]

    # The main model is probed first even when an image model is configured:
    # handing the image straight to the model that is reasoning about it always
    # beats reading someone else's transcription of it.
    if await vision_support(llm) is VisionSupport.SUPPORTED:
        blocks: list[dict[str, Any]] = [
            {"type": "text", "text": _attached_header(attachments) + note}
        ]
        blocks.extend(
            {"type": "image_url", "image_url": {"url": attachment.data_url}}
            for attachment in attachments
        )
        return [{"role": "user", "content": blocks}]

    if vision_llm is not None:
        described = await _describe(vision_llm, attachments)
        if described:
            return [{"role": "user", "content": described + note}]

    return [{
        "role": "system",
        "content": (
            "The user attached "
            + ", ".join(attachment.path.name for attachment in attachments)
            + ", but neither the active model nor any configured image model can "
            "read images, so you have not received them. Say so plainly in the "
            "user's language, mention that an image model can be set in setup, "
            "and offer to continue from a text description instead. Do not guess "
            "at the contents." + note
        ),
    }]


# ---------------------------------------------------------------------------


def _resolve(raw: str, working_dir: Path | None) -> Path | None:
    """Find the longest suffix of a candidate span that is an existing file.

    The span may carry leading prose the pattern could not separate, so each
    plausible beginning is tried left to right — longest first, so the fullest
    path wins over a bare tail that happens to exist:

        "看看/Users/me/x.png"        -> "/Users/me/x.png"
        "check /tmp/my shot.png ok"  -> "/tmp/my shot.png"
    """
    candidate = raw.replace("\\ ", " ").strip()
    # A path can begin at a separator (the CJK-glue case) or just after
    # whitespace or a quote (the prose case).
    starts = [0] + [
        index
        for index, char in enumerate(candidate)
        if index and (char in "/~" or candidate[index - 1] in " \t\"'`")
    ]
    for start in starts[:_MAX_STARTS]:
        stripped = candidate[start:].strip("\"'` \t")
        if not stripped:
            continue
        path = Path(stripped).expanduser()
        if not path.is_absolute() and working_dir is not None:
            path = working_dir / path
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    return None


def _load(path: Path) -> ImageAttachment | None:
    media_type = _MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        return None
    try:
        if path.stat().st_size > MAX_IMAGE_BYTES:
            logger.info("skipping %s: larger than %d bytes", path.name, MAX_IMAGE_BYTES)
            return None
        data = path.read_bytes()
    except OSError:
        logger.info("skipping %s: unreadable", path.name, exc_info=True)
        return None
    return ImageAttachment(path, media_type, base64.b64encode(data).decode("ascii"))


def _attached_header(attachments: list[ImageAttachment]) -> str:
    names = ", ".join(attachment.path.name for attachment in attachments)
    return f"[Attached image(s): {names}]"


def _skipped_note(skipped: list[str]) -> str:
    if not skipped:
        return ""
    return (
        f"\n[Could not attach: {', '.join(skipped)} — unreadable, unsupported, "
        f"over {MAX_IMAGE_BYTES // (1024 * 1024)}MB, or past the {MAX_IMAGES}-image limit.]"
    )


async def _describe(vision_llm: Any, attachments: list[ImageAttachment]) -> str:
    """Transcribe each image with the configured image model."""
    parts: list[str] = []
    for attachment in attachments:
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": _DESCRIBE_PROMPT},
                {"type": "image_url", "image_url": {"url": attachment.data_url}},
            ],
        }]
        try:
            with llm_purpose("vision_describe"):
                response = await vision_llm.chat(messages)
        except Exception:
            # One unreadable image must not sink the others, and it must not
            # sink the run either — the user still asked a question.
            logger.warning("image model failed on %s", attachment.path.name, exc_info=True)
            continue
        text = (getattr(response, "text", "") or "").strip()
        if text:
            parts.append(f"[Description of attached image {attachment.path.name}]\n{text}")
    return "\n\n".join(parts)
