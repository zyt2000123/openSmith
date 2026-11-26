"""Shared secret-redaction primitives across safety, context, and observability.

Single source of truth for sensitive key detection, embedded credential
patterns, CLI flag-value redaction, URL userinfo scrubbing, and single-line
sanitization.  ``approval.py``, ``trace_store.py``, and ``action_ledger.py``
all delegate here so redaction rules cannot drift across layers.
"""

from __future__ import annotations

import re

SENSITIVE_ARGUMENT_KEY_PARTS = (
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "passwd",
    "password",
    "privatekey",
    "secret",
    "token",
)

SECRET_FLAG_RE = re.compile(
    r"^-{1,2}(?:password|passwd|token|secret|api[-_]?key|apikey|"
    r"access[-_]?key|client[-_]?secret|private[-_]?key|auth(?:orization)?|"
    r"bearer|session[-_]?key|cookie)(?:=|$)",
    re.IGNORECASE,
)

PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)"
)

SECRET_VALUE_RE = re.compile(
    r"(?:"
    r"sk-[A-Za-z0-9_\-]{8,}"
    r"|gh[pousr]_[A-Za-z0-9]{10,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|glpat-[A-Za-z0-9_\-]{16,}"
    r"|xox[baprs]-[A-Za-z0-9\-]{8,}"
    r"|AKIA[0-9A-Z]{12,}"
    r"|AIza[0-9A-Za-z_\-]{30,}"
    r"|eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"
    r"|Bearer [A-Za-z0-9._~+/=\-]+"
    r"|Basic [A-Za-z0-9._~+/=\-]+"
    r")"
)

# Search-mode pattern for credentials embedded inside longer strings (shell
# commands, URLs, JSON blobs).  The leading (?<![A-Za-z0-9]) prevents false
# positives inside kebab-case words like ``task-scheduler`` or ``disk-usage``.
EMBEDDED_SECRET_RE = re.compile(
    r"(?:"
    r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{16,}"
    r"|(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{20,}"
    r"|(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{20,}"
    r"|(?<![A-Za-z0-9])glpat-[A-Za-z0-9_\-]{16,}"
    r"|(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9\-]{10,}"
    r"|(?<![A-Za-z0-9])AKIA[0-9A-Z]{12,}"
    r"|(?<![A-Za-z0-9])AIza[0-9A-Za-z_\-]{30,}"
    r"|(?<![A-Za-z0-9])eyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{1,}\.[A-Za-z0-9_\-]{1,}"
    # Bare Bearer/Basic token that is not itself one of the key families above.
    r"|\bBearer\s+(?!sk-|gh[pousr]_|github_pat_|glpat-|xox|AKIA|AIza|eyJ)"
    r"[A-Za-z0-9._~+/=\-]{8,}"
    r"|\bBasic\s+(?=[A-Za-z0-9._~+/=\-]*[0-9])[A-Za-z0-9._~+/=\-]{12,}"
    r")"
)

# Trace-store secret pattern preserves the stricter digit requirement on
# case-insensitive bearer/basic headers so prose like "Basic authentication"
# is never falsely redacted in traces.
TRACE_SECRET_RE = re.compile(
    r"""(?xi)
    (?<![A-Za-z0-9])
    (?: \b(?:bearer|basic)\s+(?=[A-Za-z0-9._~+/=-]*[0-9])
          [A-Za-z0-9._~+/=-]{16,}                                     # auth headers
      | sk-[A-Za-z0-9_-]{16,}                                         # OpenAI/Anthropic style
      | gh[pousr]_[A-Za-z0-9]{20,}                                    # GitHub classic
      | github_pat_[A-Za-z0-9_]{20,}                                  # GitHub fine-grained PAT
      | glpat-[A-Za-z0-9_-]{16,}                                      # GitLab PAT
      | AIza[0-9A-Za-z_-]{30,}                                        # Google API key
      | AKIA[0-9A-Z]{12,}                                             # AWS key id
      | xox[abprs]-[A-Za-z0-9-]{10,}                                  # Slack
      | eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+     # JWT
    )
    """
)

# ``scheme://userinfo@`` in a URL — keep the scheme and host, drop userinfo.
URL_CREDENTIALS_RE = re.compile(
    r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/@\s]+)@"
)

# ``--token value`` / ``--token=value`` embedded in a command string.
EMBEDDED_FLAG_RE = re.compile(
    r"(?:(?<=\s)|^)(-{1,2}(?:password|passwd|token|secret|api[-_]?key|apikey|"
    r"access[-_]?key|client[-_]?secret|private[-_]?key|auth(?:orization)?|"
    r"bearer|session[-_]?key|cookie))(?:\s*=\s*|\s+)(?:(?:'[^']*')|(?:\"[^\"]*\")|[^\s;|&]+)",
    re.IGNORECASE,
)


def is_sensitive_argument_name(key: object) -> bool:
    """Return True when an argument/field name looks credential-bearing."""
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return any(part in normalized for part in SENSITIVE_ARGUMENT_KEY_PARTS)


def is_secret_value(value: str) -> bool:
    """Return True when a whole string is itself a credential."""
    text = value.strip()
    if not text:
        return False
    if re.search(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", text):
        return True
    return bool(SECRET_VALUE_RE.fullmatch(text))


def redact_secret_text(text: str, *, replacement: str = "***") -> str:
    """Redact credentials embedded in human-readable summaries or logs.

    A whole-string credential is hidden entirely; otherwise embedded tokens,
    PEM private keys, ``--flag value`` pairs, and URL userinfo are scrubbed
    in place so surrounding operational context stays readable.
    """
    if not text:
        return text
    if is_secret_value(text):
        return replacement
    redacted = PRIVATE_KEY_RE.sub(replacement, text)
    redacted = EMBEDDED_SECRET_RE.sub(replacement, redacted)
    redacted = EMBEDDED_FLAG_RE.sub(rf"\1 {replacement}", redacted)
    redacted = URL_CREDENTIALS_RE.sub(rf"\1{replacement}@", redacted)
    return redacted


def redact_trace_secrets(text: str) -> str:
    """Redact credentials in persisted trace values using ``[REDACTED]``."""
    if not text:
        return text
    redacted = PRIVATE_KEY_RE.sub("[REDACTED]", text)
    redacted = TRACE_SECRET_RE.sub("[REDACTED]", redacted)
    redacted = URL_CREDENTIALS_RE.sub(r"\1[REDACTED]@", redacted)
    return redacted


def sanitize_single_line(
    text: str,
    *,
    max_chars: int = 120,
    replacement: str = "***",
) -> str:
    """Redact secrets, strip control chars/newlines, collapse spaces, and cap length."""
    redacted = redact_secret_text(text, replacement=replacement)
    cleaned = "".join(
        char if ord(char) >= 32 and char != "\x7f" else " "
        for char in redacted
    )
    compact = " ".join(cleaned.split())
    if len(compact) <= max_chars:
        return compact
    if max_chars <= 1:
        return "…"[:max_chars]
    return compact[: max_chars - 1].rstrip() + "…"


__all__ = (
    "EMBEDDED_FLAG_RE",
    "EMBEDDED_SECRET_RE",
    "PRIVATE_KEY_RE",
    "SECRET_FLAG_RE",
    "SECRET_VALUE_RE",
    "SENSITIVE_ARGUMENT_KEY_PARTS",
    "TRACE_SECRET_RE",
    "URL_CREDENTIALS_RE",
    "is_secret_value",
    "is_sensitive_argument_name",
    "redact_secret_text",
    "redact_trace_secrets",
    "sanitize_single_line",
)
