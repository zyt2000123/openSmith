"""Local credential vault (``{{vault:NAME}}``) with boundary-only resolution and redaction.

Secrets are stored in ``~/.agent-smith/vault.json`` with ``0o600`` permissions
and symlink-safe atomic writes.  Model prompts, API listings, approval cards,
audit logs, and tool ledgers only ever see key names and metadata; values are
resolved exclusively at the tool/MCP execution boundary and scrubbed from
tool outputs and errors via :class:`VaultRedactor`.
"""

from __future__ import annotations

import base64
import fnmatch
import json
import os
import re
import tempfile
import time
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common.paths import PRIVATE_FILE_MODE, _ensure_private_dir, _ensure_real_path

VAULT_KEY_NAME_RE: re.Pattern[str] = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
VAULT_PLACEHOLDER_RE: re.Pattern[str] = re.compile(
    r"\{\{vault:([A-Z][A-Z0-9_]{1,63})\}\}"
)
_MIN_STRONG_SECRET_LEN = 8


class VaultError(ValueError):
    """Raised on invalid vault key names, missing keys, or scope violations."""


class VaultScopeError(VaultError):
    """Raised when a vault key is referenced by a disallowed tool or domain."""


@dataclass(frozen=True)
class VaultMetadata:
    """Public metadata for one vault entry (never carries the secret value)."""

    name: str
    description: str = ""
    allowed_tools: tuple[str, ...] = ()
    allowed_domains: tuple[str, ...] = ()
    updated_at: str = ""
    weak_secret: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "allowed_tools": list(self.allowed_tools),
            "allowed_domains": list(self.allowed_domains),
            "updated_at": self.updated_at,
            "weak_secret": self.weak_secret,
        }


@dataclass(frozen=True)
class VaultEntry:
    """Full vault entry (internal boundary use only)."""

    name: str
    value: str
    description: str = ""
    allowed_tools: tuple[str, ...] = ()
    allowed_domains: tuple[str, ...] = ()
    updated_at: str = ""

    def metadata(self) -> VaultMetadata:
        return VaultMetadata(
            name=self.name,
            description=self.description,
            allowed_tools=self.allowed_tools,
            allowed_domains=self.allowed_domains,
            updated_at=self.updated_at,
            weak_secret=len(self.value) < _MIN_STRONG_SECRET_LEN,
        )


def validate_vault_key_name(name: str) -> str:
    if not isinstance(name, str) or not VAULT_KEY_NAME_RE.fullmatch(name):
        raise VaultError(
            "Vault key name must start with an uppercase ASCII letter and contain "
            f"only A-Z, 0-9, '_' (2-64 chars), got {name!r}"
        )
    return name


def _normalize_domain(domain: str) -> str:
    return domain.strip().lower().rstrip(".")


def _domain_matches(host: str, pattern: str) -> bool:
    norm_host = _normalize_domain(host)
    norm_pat = _normalize_domain(pattern)
    if not norm_host or not norm_pat:
        return False
    if norm_pat.startswith("*."):
        suffix = norm_pat[2:]
        if "." not in suffix:
            return False
        return norm_host.endswith(f".{suffix}") and len(norm_host) > len(suffix) + 1
    return norm_host == norm_pat


def extract_vault_placeholders(obj: Any) -> list[str]:
    """Return ordered unique vault key names referenced anywhere inside ``obj``."""
    found: dict[str, None] = {}
    stack: list[Any] = [obj]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            for match in VAULT_PLACEHOLDER_RE.finditer(current):
                found.setdefault(match.group(1), None)
        elif isinstance(current, Mapping):
            # Reverse so pop() processes keys in declaration order
            stack.extend(reversed(list(current.values())))
        elif isinstance(current, (list, tuple)):
            stack.extend(reversed(list(current)))
    return list(found)


class VaultStore:
    """File-backed credential store with ``0o600`` atomic persistence."""

    def __init__(self, vault_path: Path | None = None) -> None:
        self.path = (
            vault_path if vault_path is not None else Path.home() / ".agent-smith" / "vault.json"
        ).expanduser()

    def _load_raw(self) -> dict[str, VaultEntry]:
        if not self.path.exists() and not self.path.is_symlink():
            return {}
        _ensure_real_path(self.path, label="vault file")
        if not self.path.is_file():
            raise VaultError(f"Vault path is not a regular file: {self.path}")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise VaultError(f"Failed to read vault file: {exc}") from exc
        if not isinstance(raw, dict):
            raise VaultError("Vault file must contain a JSON object")
        entries_raw = raw.get("entries", {})
        if not isinstance(entries_raw, dict):
            raise VaultError("Vault entries must be a JSON object")
        entries: dict[str, VaultEntry] = {}
        for name, item in entries_raw.items():
            if not isinstance(name, str) or not VAULT_KEY_NAME_RE.fullmatch(name):
                continue
            if not isinstance(item, dict) or not isinstance(item.get("value"), str):
                continue
            allowed_tools = tuple(
                str(t) for t in (item.get("allowed_tools") or ()) if isinstance(t, str) and t
            )
            allowed_domains = tuple(
                str(d) for d in (item.get("allowed_domains") or ()) if isinstance(d, str) and d
            )
            entries[name] = VaultEntry(
                name=name,
                value=item["value"],
                description=str(item.get("description") or ""),
                allowed_tools=allowed_tools,
                allowed_domains=allowed_domains,
                updated_at=str(item.get("updated_at") or ""),
            )
        return entries

    def _save_raw(self, entries: dict[str, VaultEntry]) -> None:
        _ensure_private_dir(self.path.parent)
        _ensure_real_path(self.path, label="vault file")
        payload = {
            "version": 1,
            "entries": {
                name: {
                    "value": entry.value,
                    "description": entry.description,
                    "allowed_tools": list(entry.allowed_tools),
                    "allowed_domains": list(entry.allowed_domains),
                    "updated_at": entry.updated_at,
                }
                for name, entry in sorted(entries.items())
            },
        }
        encoded = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

        # Atomic 0o600 write: O_CREAT|O_EXCL prevents pre-existing symlink hijack
        # and guarantees no wide-permission window ever exists on disk.
        fd, tmp_name = tempfile.mkstemp(
            prefix=".vault.",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        tmp_path = Path(tmp_name)
        try:
            os.fchmod(fd, PRIVATE_FILE_MODE)
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
            os.chmod(self.path, PRIVATE_FILE_MODE)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def list_metadata(self) -> list[VaultMetadata]:
        return [entry.metadata() for _, entry in sorted(self._load_raw().items())]

    def get_metadata(self, name: str) -> VaultMetadata | None:
        entry = self._load_raw().get(name)
        return entry.metadata() if entry is not None else None

    def get_entry(self, name: str) -> VaultEntry | None:
        return self._load_raw().get(name)

    def all_entries(self) -> dict[str, VaultEntry]:
        return self._load_raw()

    def set_secret(
        self,
        name: str,
        value: str,
        *,
        description: str = "",
        allowed_tools: Iterable[str] = (),
        allowed_domains: Iterable[str] = (),
    ) -> VaultMetadata:
        validate_vault_key_name(name)
        if not isinstance(value, str) or not value:
            raise VaultError("Vault secret value must be a non-empty string")
        clean_tools = tuple(t.strip() for t in allowed_tools if isinstance(t, str) and t.strip())
        clean_domains = tuple(
            _normalize_domain(d)
            for d in allowed_domains
            if isinstance(d, str) and d.strip()
        )
        for domain in clean_domains:
            if domain in {"*", "*.*"} or (domain.startswith("*.") and "." not in domain[2:]):
                raise VaultError(f"Overly broad vault allowed_domain rejected: {domain!r}")
        entries = self._load_raw()
        entry = VaultEntry(
            name=name,
            value=value,
            description=str(description or "").strip(),
            allowed_tools=clean_tools,
            allowed_domains=clean_domains,
            updated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        )
        entries[name] = entry
        self._save_raw(entries)
        return entry.metadata()

    def delete_secret(self, name: str) -> bool:
        validate_vault_key_name(name)
        entries = self._load_raw()
        if name not in entries:
            return False
        del entries[name]
        self._save_raw(entries)
        return True

    def _check_entry_scope(
        self,
        entry: VaultEntry,
        *,
        tool_name: str | None,
        target_domain: str | None,
        require_explicit_mcp_scope: bool = True,
    ) -> None:
        if (
            require_explicit_mcp_scope
            and tool_name is not None
            and tool_name.startswith("mcp_")
            and not entry.allowed_tools
        ):
            raise VaultScopeError(
                f"[vault-scope-001] Vault key '{entry.name}' must declare "
                f"allowed_tools before it can be used by MCP tool '{tool_name}'"
            )
        if entry.allowed_tools and tool_name is not None:
            if not any(fnmatch.fnmatchcase(tool_name, pat) for pat in entry.allowed_tools):
                raise VaultScopeError(
                    f"[vault-scope-001] Vault key '{entry.name}' is not authorized "
                    f"for tool '{tool_name}'"
                )
        if entry.allowed_domains:
            if not target_domain:
                if tool_name is not None:
                    raise VaultScopeError(
                        f"[vault-scope-001] Vault key '{entry.name}' requires an "
                        f"authorized target domain ({', '.join(entry.allowed_domains)})"
                    )
            elif not any(_domain_matches(target_domain, pat) for pat in entry.allowed_domains):
                raise VaultScopeError(
                    f"[vault-scope-001] Vault key '{entry.name}' is not authorized "
                    f"for domain '{target_domain}'"
                )

    def validate_placeholders(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        target_domain: str | None = None,
    ) -> tuple[bool, str, list[str]]:
        """Validate that all vault placeholders in ``arguments`` exist and are in scope."""
        keys = extract_vault_placeholders(arguments)
        if not keys:
            return True, "", []
        entries = self._load_raw()
        for key in keys:
            entry = entries.get(key)
            if entry is None:
                return False, f"[vault-scope-001] Unknown vault key: {key}", keys
            try:
                self._check_entry_scope(
                    entry, tool_name=tool_name, target_domain=target_domain
                )
            except VaultScopeError as exc:
                return False, str(exc), keys
        return True, "", keys

    def resolve_string(
        self,
        text: str,
        *,
        tool_name: str | None = None,
        target_domain: str | None = None,
        entries: Mapping[str, VaultEntry] | None = None,
        require_explicit_mcp_scope: bool = True,
    ) -> tuple[str, list[tuple[str, str]]]:
        """Resolve ``{{vault:NAME}}`` placeholders in ``text`` at the execution boundary."""
        if "{{vault:" not in text:
            return text, []
        loaded = entries if entries is not None else self._load_raw()
        used: dict[str, str] = {}

        def _replace(match: re.Match[str]) -> str:
            key = match.group(1)
            entry = loaded.get(key)
            if entry is None:
                raise VaultError(f"[vault-scope-001] Unknown vault key: {key}")
            self._check_entry_scope(
                entry,
                tool_name=tool_name,
                target_domain=target_domain,
                require_explicit_mcp_scope=require_explicit_mcp_scope,
            )
            used[key] = entry.value
            return entry.value

        resolved = VAULT_PLACEHOLDER_RE.sub(_replace, text)
        return resolved, list(used.items())

    def resolve_arguments(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        target_domain: str | None = None,
    ) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        """Deep-resolve vault placeholders into a fresh dict without mutating ``arguments``."""
        keys = extract_vault_placeholders(arguments)
        if not keys:
            return dict(arguments), []
        loaded = self._load_raw()
        used: dict[str, str] = {}

        def _walk(value: Any) -> Any:
            if isinstance(value, str):
                resolved, pairs = self.resolve_string(
                    value,
                    tool_name=tool_name,
                    target_domain=target_domain,
                    entries=loaded,
                )
                for k, v in pairs:
                    used[k] = v
                return resolved
            if isinstance(value, Mapping):
                return {k: _walk(v) for k, v in value.items()}
            if isinstance(value, list):
                return [_walk(item) for item in value]
            if isinstance(value, tuple):
                return tuple(_walk(item) for item in value)
            return value

        resolved_dict = {k: _walk(v) for k, v in arguments.items()}
        return resolved_dict, list(used.items())

    def prompt_summary(self) -> str:
        """Safe model-visible summary listing only key names and descriptions."""
        items = self.list_metadata()
        if not items:
            return ""
        parts = [
            f"{m.name} ({m.description})" if m.description else m.name
            for m in items
        ]
        return ", ".join(parts)


class VaultRedactor:
    """Reverse-redacts vault secret values (plain, URL-encoded, Base64) from outputs."""

    @staticmethod
    def redact(
        text: str,
        resolved_secrets: Iterable[tuple[str, str]] = (),
        all_entries: Mapping[str, VaultEntry] | None = None,
    ) -> tuple[str, list[str]]:
        if not text:
            return text, []
        resolved_map = dict(resolved_secrets)
        candidates: dict[str, str] = dict(resolved_map)
        if all_entries:
            for name, entry in all_entries.items():
                candidates.setdefault(name, entry.value)

        # Build (needle, replacement, key_name, word_boundary_only) tuples
        replacements: list[tuple[str, str, str, bool]] = []
        for name, secret in candidates.items():
            if not secret:
                continue
            marker = f"[REDACTED:vault:{name}]"
            if len(secret) >= _MIN_STRONG_SECRET_LEN:
                replacements.append((secret, marker, name, False))
                url_enc = urllib.parse.quote(secret, safe="")
                if url_enc != secret:
                    replacements.append((url_enc, marker, name, False))
                b64_enc = base64.b64encode(secret.encode("utf-8")).decode("ascii")
                if b64_enc and b64_enc != secret:
                    replacements.append((b64_enc, marker, name, False))
            elif name in resolved_map:
                # Weak (<8 chars) secret: redact only when explicitly resolved in
                # this call, on word boundaries to avoid corrupting normal text.
                replacements.append((secret, marker, name, True))

        # Longest match first so longer secrets are redacted cleanly before shorter substrings
        replacements.sort(key=lambda item: len(item[0]), reverse=True)

        redacted = text
        matched_keys: dict[str, None] = {}
        for needle, marker, key_name, word_only in replacements:
            if word_only:
                pattern = re.compile(rf"(?<!\w){re.escape(needle)}(?!\w)")
                new_text, count = pattern.subn(marker, redacted)
                if count > 0:
                    redacted = new_text
                    matched_keys.setdefault(key_name, None)
            elif needle in redacted:
                redacted = redacted.replace(needle, marker)
                matched_keys.setdefault(key_name, None)

        return redacted, list(matched_keys)


__all__ = (
    "VAULT_KEY_NAME_RE",
    "VAULT_PLACEHOLDER_RE",
    "VaultEntry",
    "VaultError",
    "VaultMetadata",
    "VaultRedactor",
    "VaultScopeError",
    "VaultStore",
    "extract_vault_placeholders",
    "validate_vault_key_name",
)
