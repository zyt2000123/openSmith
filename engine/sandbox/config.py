"""Unified sandbox profile configuration and resolution."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from common.sensitive_rules import SAFE_AGENT_SMITH_RO_REL_DIRS, runtime_credential_paths

from .policy import (
    assert_no_control_characters,
    is_hardlink_protected_path,
    validate_configured_root,
)

SandboxBackendChoice = Literal["auto", "seatbelt", "bwrap"]
SandboxFallbackPolicy = Literal["deny", "host_with_approval"]
DefaultNetworkMode = Literal["loopback_only", "none"]

_VALID_BACKENDS = frozenset({"auto", "seatbelt", "bwrap"})
_VALID_FALLBACK_POLICIES = frozenset({"deny", "host_with_approval"})
_VALID_DEFAULT_NETWORKS = frozenset({"loopback_only", "none"})
_ALLOWED_CONFIG_KEYS = frozenset({
    "backend",
    "fallback_policy",
    "extra_roots",
    "auto_detect_toolchains",
    "share_read_only",
    "auto_detect_caches",
    "cache_roots",
    "extra_path_entries",
    "default_network",
})
_ALLOWED_CACHE_KINDS = frozenset({"uv", "pip", "npm", "pnpm", "cargo", "go", "extra"})


@dataclass(frozen=True)
class CacheMount:
    """A validated read-write package cache mount and its environment variables."""

    kind: str
    host_path: Path
    env_vars: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SandboxProfileConfig:
    """User-declared sandbox configuration parsed from ``~/.agent-smith/config.yaml``."""

    backend: SandboxBackendChoice = "auto"
    fallback_policy: SandboxFallbackPolicy = "deny"
    extra_roots: tuple[str, ...] = ()
    auto_detect_toolchains: bool = True
    share_read_only: tuple[str, ...] = ()
    auto_detect_caches: bool = True
    cache_roots: dict[str, str | list[str]] = field(default_factory=dict)
    extra_path_entries: tuple[str, ...] = ()
    default_network: DefaultNetworkMode = "loopback_only"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object] | None) -> SandboxProfileConfig:
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise ValueError("sandbox configuration must be a mapping")

        unknown = set(raw.keys()) - _ALLOWED_CONFIG_KEYS
        if unknown:
            raise ValueError(
                f"unknown sandbox configuration keys: {', '.join(sorted(unknown))}"
            )

        backend = raw.get("backend", "auto")
        if not isinstance(backend, str) or backend not in _VALID_BACKENDS:
            raise ValueError(
                f"sandbox.backend must be one of {sorted(_VALID_BACKENDS)}, got {backend!r}"
            )

        fallback_policy = raw.get("fallback_policy", "deny")
        if (
            not isinstance(fallback_policy, str)
            or fallback_policy not in _VALID_FALLBACK_POLICIES
        ):
            raise ValueError(
                "sandbox.fallback_policy must be one of "
                f"{sorted(_VALID_FALLBACK_POLICIES)}, got {fallback_policy!r}"
            )

        default_network = raw.get("default_network", "loopback_only")
        if (
            not isinstance(default_network, str)
            or default_network not in _VALID_DEFAULT_NETWORKS
        ):
            raise ValueError(
                "sandbox.default_network must be one of "
                f"{sorted(_VALID_DEFAULT_NETWORKS)}, got {default_network!r}"
            )

        auto_detect_toolchains = raw.get("auto_detect_toolchains", True)
        if not isinstance(auto_detect_toolchains, bool):
            raise ValueError("sandbox.auto_detect_toolchains must be a boolean")

        auto_detect_caches = raw.get("auto_detect_caches", True)
        if not isinstance(auto_detect_caches, bool):
            raise ValueError("sandbox.auto_detect_caches must be a boolean")

        extra_roots = _parse_str_list(raw.get("extra_roots", ()), "sandbox.extra_roots")
        share_read_only = _parse_str_list(
            raw.get("share_read_only", ()), "sandbox.share_read_only"
        )
        extra_path_entries = _parse_str_list(
            raw.get("extra_path_entries", ()), "sandbox.extra_path_entries"
        )

        raw_caches = raw.get("cache_roots", {})
        if raw_caches is None:
            raw_caches = {}
        if not isinstance(raw_caches, Mapping):
            raise ValueError("sandbox.cache_roots must be a mapping")
        cache_roots: dict[str, str | list[str]] = {}
        for kind, val in raw_caches.items():
            if not isinstance(kind, str) or kind not in _ALLOWED_CACHE_KINDS:
                raise ValueError(
                    f"sandbox.cache_roots key must be one of {sorted(_ALLOWED_CACHE_KINDS)}, got {kind!r}"
                )
            if val is None or val == "":
                continue
            if kind == "extra":
                cache_roots[kind] = list(_parse_str_list(val, "sandbox.cache_roots.extra"))
            else:
                if not isinstance(val, str):
                    raise ValueError(f"sandbox.cache_roots.{kind} must be a path string")
                assert_no_control_characters(val, label=f"sandbox.cache_roots.{kind}")
                cache_roots[kind] = val

        return cls(
            backend=backend,  # type: ignore[arg-type]
            fallback_policy=fallback_policy,  # type: ignore[arg-type]
            extra_roots=extra_roots,
            auto_detect_toolchains=auto_detect_toolchains,
            share_read_only=share_read_only,
            auto_detect_caches=auto_detect_caches,
            cache_roots=cache_roots,
            extra_path_entries=extra_path_entries,
            default_network=default_network,  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "fallback_policy": self.fallback_policy,
            "extra_roots": list(self.extra_roots),
            "auto_detect_toolchains": self.auto_detect_toolchains,
            "share_read_only": list(self.share_read_only),
            "auto_detect_caches": self.auto_detect_caches,
            "cache_roots": {
                k: list(v) if isinstance(v, list) else v
                for k, v in self.cache_roots.items()
            },
            "extra_path_entries": list(self.extra_path_entries),
            "default_network": self.default_network,
        }


def _parse_str_list(raw: object, field_name: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"{field_name} must be a list of path strings")
    result: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_name} entries must be non-empty strings")
        assert_no_control_characters(item, label=field_name)
        result.append(item)
    return tuple(result)


@dataclass(frozen=True)
class ResolvedSandboxProfile:
    """Validated, immutable runtime sandbox profile consumed by Seatbelt and bwrap."""

    workspace: Path
    extra_roots: tuple[Path, ...] = ()
    share_read_only: tuple[Path, ...] = ()
    cache_mounts: tuple[CacheMount, ...] = ()
    path_entries: tuple[Path, ...] = ()
    runtime_secret_paths: tuple[Path, ...] = ()
    non_delegable_write_paths: tuple[Path, ...] = ()
    default_network: DefaultNetworkMode = "loopback_only"
    fallback_policy: SandboxFallbackPolicy = "deny"
    backend: SandboxBackendChoice = "auto"
    warnings: tuple[str, ...] = ()
    rejected_paths: tuple[str, ...] = ()

    @property
    def rw_workspace_roots(self) -> tuple[Path, ...]:
        """Workspace plus configured multi-workspace extra_roots."""
        return (self.workspace, *self.extra_roots)

    @property
    def all_writable_roots(self) -> tuple[Path, ...]:
        """All sandbox-writable roots (workspace + extra_roots + cache_mounts)."""
        return (*self.rw_workspace_roots, *(m.host_path for m in self.cache_mounts))

    def cache_env_vars(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for mount in self.cache_mounts:
            for key, value in mount.env_vars:
                env[key] = value
        return env


def _dedupe_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    seen: dict[Path, None] = {}
    for p in paths:
        seen.setdefault(p, None)
    return tuple(seen)


def _auto_toolchain_candidates(
    *,
    platform: str,
    home: Path,
    data_dir: Path,
) -> list[Path]:
    candidates: list[Path] = []
    if platform == "darwin":
        candidates.extend([
            Path("/opt/homebrew"),
            Path("/usr/local/Homebrew"),
            Path("/usr/local/bin"),
            Path("/usr/local/lib"),
            Path("/usr/local/opt"),
        ])
    else:
        candidates.append(Path("/usr/local"))

    candidates.extend([
        home / ".local" / "share" / "uv",
        home / ".local" / "bin",
        Path(sys.prefix),
        Path(sys.base_prefix),
        home / ".nvm" / "versions",
        home / ".pyenv" / "versions",
        home / ".rustup",
        home / ".cargo" / "bin",
    ])
    for rel_parts in SAFE_AGENT_SMITH_RO_REL_DIRS:
        candidates.append(data_dir.joinpath(*rel_parts))
    return candidates


def _cache_env_for(kind: str, path: Path) -> tuple[tuple[str, str], ...]:
    rendered = str(path)
    if kind == "uv":
        return (("UV_CACHE_DIR", rendered),)
    if kind == "pip":
        return (("PIP_CACHE_DIR", rendered),)
    if kind == "npm":
        return (("NPM_CONFIG_CACHE", rendered),)
    if kind == "pnpm":
        return (("npm_config_store_dir", rendered),)
    if kind == "go":
        if path.name == "go-build":
            return (("GOCACHE", rendered),)
        return (("GOMODCACHE", rendered),)
    return ()


def _auto_cache_candidates(*, platform: str, home: Path) -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []
    if platform == "darwin":
        candidates.extend([
            ("uv", home / "Library" / "Caches" / "uv"),
            ("uv", home / ".cache" / "uv"),
            ("pip", home / "Library" / "Caches" / "pip"),
            ("pip", home / ".cache" / "pip"),
            ("pnpm", home / "Library" / "pnpm" / "store"),
            ("pnpm", home / ".local" / "share" / "pnpm" / "store"),
        ])
    else:
        candidates.extend([
            ("uv", home / ".cache" / "uv"),
            ("pip", home / ".cache" / "pip"),
            ("pnpm", home / ".local" / "share" / "pnpm" / "store"),
        ])
    candidates.extend([
        ("npm", home / ".npm"),
        ("cargo", home / ".cargo" / "registry"),
        ("go", home / "go" / "pkg" / "mod"),
        ("go", home / ".cache" / "go-build"),
    ])
    return candidates


def _is_path_allowed_for_exec(
    entry: Path,
    *,
    allowed_bases: tuple[Path, ...],
) -> bool:
    for base in allowed_bases:
        try:
            if entry == base or entry.is_relative_to(base):
                return True
        except ValueError:
            continue
    return False


def resolve_sandbox_profile(
    workspace: str | Path,
    config: SandboxProfileConfig | None = None,
    *,
    runtime_secret_paths: Iterable[str | Path] | None = None,
    non_delegable_write_paths: Iterable[str | Path] = (),
    home: Path | None = None,
    data_dir: Path | None = None,
    platform: str | None = None,
) -> ResolvedSandboxProfile:
    """Resolve and validate a :class:`SandboxProfileConfig` for one workspace."""
    cfg = config if config is not None else SandboxProfileConfig()
    active_platform = platform or sys.platform
    resolved_home = (home if home is not None else Path.home()).expanduser().resolve()
    resolved_data = (
        data_dir if data_dir is not None else resolved_home / ".agent-smith"
    ).expanduser().resolve()

    assert_no_control_characters(workspace, label="sandbox workspace path")
    resolved_workspace = Path(workspace).expanduser().resolve()
    assert_no_control_characters(resolved_workspace, label="sandbox workspace path")
    if is_hardlink_protected_path(resolved_workspace):
        raise ValueError(
            f"sandbox workspace is inside a protected directory: {resolved_workspace}"
        )

    configured_secrets = (
        runtime_credential_paths(resolved_data)
        if runtime_secret_paths is None
        else tuple(Path(p).expanduser().resolve() for p in runtime_secret_paths)
    )
    for secret in configured_secrets:
        assert_no_control_characters(secret, label="runtime secret path")

    resolved_non_delegable = tuple(
        Path(p).expanduser().resolve() for p in non_delegable_write_paths
    )
    for nd in resolved_non_delegable:
        assert_no_control_characters(nd, label="non-delegable write path")

    warnings: list[str] = []
    rejected: list[str] = []

    def _record_issue(reason: str, *, explicit: bool) -> None:
        if "[SANDBOX_CONFIG_PATH_REJECTED]" in reason:
            rejected.append(reason)
            warnings.append(reason)
        elif explicit:
            warnings.append(reason)

    # 1. Extra workspace roots (RW)
    extra_roots: list[Path] = []
    for raw in cfg.extra_roots:
        resolved, err = validate_configured_root(
            raw,
            role="extra_root",
            runtime_secret_paths=configured_secrets,
            non_delegable_write_paths=resolved_non_delegable,
            home=resolved_home,
            data_dir=resolved_data,
        )
        if err:
            _record_issue(err, explicit=True)
            continue
        assert resolved is not None
        if resolved != resolved_workspace:
            extra_roots.append(resolved)

    # 2. Controlled read-only shares (explicit + auto-detected toolchains)
    ro_roots: list[Path] = []
    for raw in cfg.share_read_only:
        resolved, err = validate_configured_root(
            raw,
            role="share_read_only",
            runtime_secret_paths=configured_secrets,
            non_delegable_write_paths=resolved_non_delegable,
            home=resolved_home,
            data_dir=resolved_data,
        )
        if err:
            _record_issue(err, explicit=True)
            continue
        assert resolved is not None
        ro_roots.append(resolved)

    if cfg.auto_detect_toolchains:
        for candidate in _auto_toolchain_candidates(
            platform=active_platform,
            home=resolved_home,
            data_dir=resolved_data,
        ):
            if not candidate.exists():
                continue
            resolved, err = validate_configured_root(
                candidate,
                role="share_read_only",
                runtime_secret_paths=configured_secrets,
                non_delegable_write_paths=resolved_non_delegable,
                home=resolved_home,
                data_dir=resolved_data,
            )
            if resolved is not None:
                ro_roots.append(resolved)

    # 3. Controlled RW cache mounts (explicit + auto-detected caches)
    cache_mounts: list[CacheMount] = []
    seen_cache_paths: set[Path] = set()
    explicit_cache_kinds: set[str] = set()

    for kind, raw_val in cfg.cache_roots.items():
        explicit_cache_kinds.add(kind)
        items = raw_val if isinstance(raw_val, list) else [raw_val]
        for raw in items:
            resolved, err = validate_configured_root(
                raw,
                role="cache_root",
                runtime_secret_paths=configured_secrets,
                non_delegable_write_paths=resolved_non_delegable,
                home=resolved_home,
                data_dir=resolved_data,
            )
            if err:
                _record_issue(err, explicit=True)
                continue
            assert resolved is not None
            if resolved in seen_cache_paths:
                continue
            seen_cache_paths.add(resolved)
            cache_mounts.append(
                CacheMount(
                    kind=kind,
                    host_path=resolved,
                    env_vars=_cache_env_for(kind, resolved),
                )
            )

    if cfg.auto_detect_caches:
        detected_kinds: set[str] = set(explicit_cache_kinds)
        for kind, candidate in _auto_cache_candidates(
            platform=active_platform,
            home=resolved_home,
        ):
            # Allow both go mod cache and go-build cache; for other kinds take
            # the first existing platform-preferred location.
            if kind != "go" and kind in detected_kinds:
                continue
            if not candidate.exists():
                continue
            resolved, err = validate_configured_root(
                candidate,
                role="cache_root",
                runtime_secret_paths=configured_secrets,
                non_delegable_write_paths=resolved_non_delegable,
                home=resolved_home,
                data_dir=resolved_data,
            )
            if resolved is None or resolved in seen_cache_paths:
                continue
            seen_cache_paths.add(resolved)
            detected_kinds.add(kind)
            cache_mounts.append(
                CacheMount(
                    kind=kind,
                    host_path=resolved,
                    env_vars=_cache_env_for(kind, resolved),
                )
            )

    deduped_extra = _dedupe_paths(extra_roots)
    deduped_ro = _dedupe_paths(ro_roots)

    # 4. Safe PATH entries (must lie inside system defaults, workspace,
    #    extra_roots, or share_read_only).
    default_sys_paths = tuple(
        Path(p) for p in os.defpath.split(os.pathsep) if p
    )
    allowed_exec_bases = (
        *default_sys_paths,
        Path("/usr/bin"),
        Path("/bin"),
        Path("/usr/sbin"),
        Path("/sbin"),
        resolved_workspace,
        *deduped_extra,
        *deduped_ro,
    )

    path_candidates: list[Path] = []
    for raw_entry in cfg.extra_path_entries:
        try:
            entry = Path(raw_entry).expanduser().resolve()
        except OSError:
            warnings.append(f"extra_path_entry cannot be resolved: {raw_entry}")
            continue
        if not entry.is_dir():
            continue
        if _is_path_allowed_for_exec(entry, allowed_bases=allowed_exec_bases):
            path_candidates.append(entry)
        else:
            msg = (
                "[SANDBOX_CONFIG_PATH_REJECTED] extra_path_entry is outside "
                f"system/workspace/share_read_only roots: {entry}"
            )
            rejected.append(msg)
            warnings.append(msg)

    if cfg.auto_detect_toolchains:
        auto_bins = [
            Path("/opt/homebrew/bin"),
            Path("/opt/homebrew/sbin"),
            Path("/usr/local/bin"),
            resolved_home / ".local" / "bin",
            resolved_home / ".cargo" / "bin",
            Path(sys.prefix) / "bin",
        ]
        for candidate in auto_bins:
            try:
                resolved_bin = candidate.resolve()
            except OSError:
                continue
            if resolved_bin.is_dir() and _is_path_allowed_for_exec(
                resolved_bin, allowed_bases=allowed_exec_bases
            ):
                path_candidates.append(resolved_bin)

    path_candidates.extend(default_sys_paths)

    return ResolvedSandboxProfile(
        workspace=resolved_workspace,
        extra_roots=deduped_extra,
        share_read_only=deduped_ro,
        cache_mounts=tuple(cache_mounts),
        path_entries=_dedupe_paths(path_candidates),
        runtime_secret_paths=configured_secrets,
        non_delegable_write_paths=resolved_non_delegable,
        default_network=cfg.default_network,
        fallback_policy=cfg.fallback_policy,
        backend=cfg.backend,
        warnings=tuple(warnings),
        rejected_paths=tuple(rejected),
    )


__all__ = (
    "CacheMount",
    "DefaultNetworkMode",
    "ResolvedSandboxProfile",
    "SandboxBackendChoice",
    "SandboxFallbackPolicy",
    "SandboxProfileConfig",
    "resolve_sandbox_profile",
)
