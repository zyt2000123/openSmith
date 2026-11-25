"""macOS Seatbelt adapter for sandboxed tool execution."""

from __future__ import annotations

import asyncio
import copy
import os
import sys
from collections.abc import Iterable
from pathlib import Path

from .config import CacheMount, ResolvedSandboxProfile
from .host import (
    CommandResult,
    LocalExecutionEnvironment,
    detect_network_denied_hint,
)
from .policy import (
    CREDENTIAL_CONFIG_FILES,
    CREDENTIAL_DIRS,
    PRIVATE_KEY_EXTENSIONS,
    SANDBOX_EXTRA_CREDENTIAL_DIRS,
    SSH_KEY_NAME_POSIX_ERE,
    SSH_KEY_NAME_RE,
    assert_no_control_characters,
    build_sbpl_sensitive_deny_clauses,
    is_hardlink_protected_path,
    is_sensitive_path,
    runtime_credential_paths,
)
from .preflight import HardlinkPreflightCache, check_hardlinks

_SANDBOX_EXECUTABLE = "/usr/bin/sandbox-exec"
_OPTIONAL_ENV_KEYS = ("LANG", "LC_ALL", "TERM", "TZ", "NO_COLOR")
_CREDENTIAL_DIRECTORIES = CREDENTIAL_DIRS | SANDBOX_EXTRA_CREDENTIAL_DIRS
_CREDENTIAL_CONFIGS = CREDENTIAL_CONFIG_FILES
_PRIVATE_KEY_SUFFIXES = frozenset(PRIVATE_KEY_EXTENSIONS)
_PRIVATE_KEY_NAME_PATTERN = SSH_KEY_NAME_POSIX_ERE
_PRIVATE_KEY_NAME_RE = SSH_KEY_NAME_RE
_DEFAULT_RUNTIME_SECRET_PATHS = runtime_credential_paths()


def _is_sensitive_data_path(path: Path) -> bool:
    """Whether shell access to this workspace-relative path is always denied."""
    return is_sensitive_path(
        path,
        include_sandbox_extra=True,
        include_env_templates=True,
    )


def _is_hardlink_protected_path(path: Path) -> bool:
    return is_hardlink_protected_path(path)


class MacOSSeatbeltEnvironment:
    """Run commands under a deny-by-default Seatbelt profile.

    By default the workspace (plus any validated ``extra_roots`` and
    ``cache_mounts``) are the only writable locations, credential data and Git
    metadata remain protected, and outbound network access is denied. A
    matching approved scope creates a copy with temporary elevated access while
    still denying Agent-Smith runtime credential paths and non-delegable write
    roots. A preflight rejects hard-link aliases that a path-based profile
    cannot distinguish. This backend fails closed on a non-macOS host or when
    ``sandbox-exec`` is unavailable.
    """

    name = "sandbox"
    backend_id = "seatbelt"

    def __init__(
        self,
        *,
        workspace: str | Path,
        runtime_secret_paths: Iterable[str | Path] | None = None,
        non_delegable_write_paths: Iterable[str | Path] = (),
        approved_host_access: bool = False,
        profile: ResolvedSandboxProfile | None = None,
    ) -> None:
        if profile is not None:
            self._workspace = profile.workspace
            self._extra_roots: tuple[Path, ...] = profile.extra_roots
            self._share_read_only: tuple[Path, ...] = profile.share_read_only
            self._toolchain_read_only: frozenset[Path] = frozenset(
                profile.toolchain_read_only
            )
            self._cache_mounts: tuple[CacheMount, ...] = profile.cache_mounts
            self._path_entries: tuple[Path, ...] = profile.path_entries
            self._default_network = profile.default_network
            configured_secrets = (
                profile.runtime_secret_paths
                if runtime_secret_paths is None
                else tuple(Path(p).expanduser().resolve() for p in runtime_secret_paths)
            )
            configured_non_delegable = (
                tuple(non_delegable_write_paths)
                if non_delegable_write_paths
                else profile.non_delegable_write_paths
            )
        else:
            self._workspace = Path(workspace).expanduser().resolve()
            self._extra_roots = ()
            self._share_read_only = ()
            self._toolchain_read_only = frozenset()
            self._cache_mounts = ()
            self._path_entries = ()
            self._default_network = "none"
            configured_secrets = (
                _DEFAULT_RUNTIME_SECRET_PATHS
                if runtime_secret_paths is None
                else tuple(Path(p).expanduser().resolve() for p in runtime_secret_paths)
            )
            configured_non_delegable = tuple(non_delegable_write_paths)

        assert_no_control_characters(self._workspace, label="sandbox workspace path")
        if _is_hardlink_protected_path(self._workspace):
            raise ValueError(
                f"sandbox workspace is inside a protected directory: {self._workspace}"
            )
        self._runtime_secret_paths = tuple(configured_secrets)
        for path in self._runtime_secret_paths:
            assert_no_control_characters(path, label="runtime secret path")
        self._non_delegable_write_paths = self._resolve_non_delegable_write_paths(
            configured_non_delegable
        )
        self._approved_host_access = approved_host_access
        self._approved_network = approved_host_access
        self._host = LocalExecutionEnvironment()
        self._preflight_cache = HardlinkPreflightCache()
        self.last_result: CommandResult | None = None

    @property
    def _rw_workspace_roots(self) -> tuple[Path, ...]:
        return (self._workspace, *self._extra_roots)

    @property
    def _all_writable_roots(self) -> tuple[Path, ...]:
        return (
            *self._rw_workspace_roots,
            *(mount.host_path for mount in self._cache_mounts),
        )

    @staticmethod
    def _resolve_non_delegable_write_paths(
        paths: Iterable[str | Path],
    ) -> tuple[Path, ...]:
        resolved = tuple(Path(path).expanduser().resolve() for path in paths)
        for path in resolved:
            assert_no_control_characters(path, label="non-delegable write path")
        return resolved

    def with_non_delegable_write_paths(
        self,
        paths: Iterable[str | Path],
    ) -> MacOSSeatbeltEnvironment:
        """Return a copy that preserves runtime-owned paths as read-only.

        This mirrors ``with_approval_scope`` so callers can retain subclasses
        that add instrumentation or controls but still bind protected roots.
        """
        scoped = copy.copy(self)
        scoped._non_delegable_write_paths = self._resolve_non_delegable_write_paths(
            paths
        )
        scoped._preflight_cache = HardlinkPreflightCache()
        return scoped

    def with_approval_scope(self, scope: object) -> MacOSSeatbeltEnvironment:
        """Return a one-call environment matching an exact approved scope.

        - ``sandbox_command``: keeps full filesystem isolation and loopback-only network.
        - ``sandbox_network``: enables outbound network while keeping full filesystem
          isolation, credential denies, and non-delegable write protection intact.
        - ``host_command``: temporary host access while still denying runtime secrets
          and non-delegable write paths.
        """
        scoped = copy.copy(self)
        if bool(getattr(scope, "grants_host_execution", False)):
            scoped._approved_host_access = True
            scoped._approved_network = True
        elif bool(getattr(scope, "grants_network_execution", False)):
            scoped._approved_host_access = False
            scoped._approved_network = True
        else:
            scoped._approved_host_access = False
            scoped._approved_network = False
        return scoped

    def _profile(self, *, network: bool = False) -> str:
        allow_outbound = bool(network or self._approved_host_access)
        runtime_denies = "\n".join(
            "(deny file-read* file-write* "
            f'(literal (param "RUNTIME_SECRET_{index}")))'
            for index, _ in enumerate(self._runtime_secret_paths)
        )
        protected_write_denies = "\n".join(
            "(deny file-write* "
            f'(subpath (param "NON_DELEGABLE_WRITE_{index}")))'
            for index, _ in enumerate(self._non_delegable_write_paths)
        )
        if self._approved_host_access:
            return f"""(version 1)
(deny default)
(import "system.sb")
(allow process*)
(allow network*)
(allow file-read* file-write*)
{runtime_denies}
{protected_write_denies}
"""
        network_clauses: list[str]
        if allow_outbound:
            network_clauses = [
                "(allow network*)",
                # Restrict unix-socket connections away from host sockets outside workspace
                "(deny network-outbound (remote unix-socket))",
                '(allow network-outbound (remote unix-socket (subpath (param "WORKSPACE"))))',
                '(allow network-outbound (remote unix-socket (literal "/private/var/run/mDNSResponder")))',
            ]
        elif self._default_network == "none":
            network_clauses = ["(deny network*)"]
        else:
            network_clauses = [
                "(deny network*)",
                '(allow network-bind (local ip "localhost:*"))',
                '(allow network-inbound (local ip "localhost:*"))',
                '(allow network-outbound (remote ip "localhost:*"))',
                '(allow network-bind (local unix-socket (subpath (param "WORKSPACE"))))',
                '(allow network-outbound (remote unix-socket (subpath (param "WORKSPACE"))))',
            ]

        parts: list[str] = [
            "(version 1)",
            "(deny default)",
            '(import "system.sb")',
            *network_clauses,
            "(allow process*)",
            '(allow file-read* (literal "/private/var/select/sh"))',
            '(allow file-read-metadata (subpath "/bin"))',
            '(allow file-read-metadata (subpath "/usr/bin"))',
            '(allow file-read* (subpath "/Applications/Xcode.app"))',
            '(allow file-read* (subpath "/Library/Developer"))',
            '(allow file-read* (literal "/private/var/db/xcode_select_link"))',
            '(allow file-read-metadata (path-ancestors (param "WORKSPACE")))',
            '(allow file-read* (subpath (param "WORKSPACE")))',
            '(allow file-write* (subpath (param "WORKSPACE")))',
        ]
        if allow_outbound:
            # Allow standard macOS TLS trust/resolver config reads when outbound
            # network is explicitly approved, without exposing user $HOME.
            parts.append('(allow file-read* (literal "/private/etc/resolv.conf"))')
            parts.append('(allow file-read* (subpath "/private/etc/ssl"))')
        for index, _ in enumerate(self._extra_roots):
            param = f"EXTRA_ROOT_{index}"
            parts.append(f'(allow file-read-metadata (path-ancestors (param "{param}")))')
            parts.append(f'(allow file-read* (subpath (param "{param}")))')
            parts.append(f'(allow file-write* (subpath (param "{param}")))')
        for index, _ in enumerate(self._share_read_only):
            param = f"SHARE_RO_{index}"
            parts.append(f'(allow file-read-metadata (path-ancestors (param "{param}")))')
            parts.append(f'(allow file-read* (subpath (param "{param}")))')
        for index, _ in enumerate(self._cache_mounts):
            param = f"CACHE_ROOT_{index}"
            parts.append(f'(allow file-read-metadata (path-ancestors (param "{param}")))')
            parts.append(f'(allow file-read* (subpath (param "{param}")))')
            parts.append(f'(allow file-write* (subpath (param "{param}")))')

        parts.append(build_sbpl_sensitive_deny_clauses("WORKSPACE").rstrip("\n"))
        for index, _ in enumerate(self._extra_roots):
            parts.append(
                build_sbpl_sensitive_deny_clauses(f"EXTRA_ROOT_{index}").rstrip("\n")
            )
        for index, ro_path in enumerate(self._share_read_only):
            # Apply sensitive-file deny rules only to user-configured share_read_only
            # directories, NOT to auto-detected toolchain trees (/opt/homebrew, uv,
            # pyenv, sys.prefix) which ship public CA bundles (*.pem) needed for TLS.
            if ro_path in self._toolchain_read_only:
                continue
            parts.append(
                build_sbpl_sensitive_deny_clauses(f"SHARE_RO_{index}").rstrip("\n")
            )
        for index, _ in enumerate(self._cache_mounts):
            parts.append(
                build_sbpl_sensitive_deny_clauses(f"CACHE_ROOT_{index}").rstrip("\n")
            )
        if runtime_denies:
            parts.append(runtime_denies)
        if protected_write_denies:
            parts.append(protected_write_denies)
        return "\n".join(parts) + "\n"

    def _validate_cwd(self, cwd: str | None) -> tuple[str | None, str | None]:
        resolved = Path(cwd).expanduser().resolve() if cwd else self._workspace
        if self._approved_host_access:
            return str(resolved), None
        for root in self._rw_workspace_roots:
            try:
                resolved.relative_to(root)
                return str(resolved), None
            except ValueError:
                continue
        return None, f"sandbox working directory escapes workspace: {resolved}"

    def _safe_environment(self, requested: dict[str, str] | None) -> dict[str, str]:
        """Build the complete child environment; never inherit server secrets."""
        path_value = (
            os.pathsep.join(str(p) for p in self._path_entries)
            if self._path_entries
            else os.defpath
        )
        environment = {
            "PATH": path_value,
            "HOME": str(Path.home() if self._approved_host_access else self._workspace),
            "TMPDIR": str(self._workspace),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "PIP_CONFIG_FILE": os.devnull,
            "NPM_CONFIG_USERCONFIG": os.devnull,
        }
        for mount in self._cache_mounts:
            for key, value in mount.env_vars:
                environment[key] = value
        if requested:
            for key in _OPTIONAL_ENV_KEYS:
                value = requested.get(key)
                if value:
                    environment[key] = value
        return environment

    def _sensitive_hardlink_error(self) -> str | None:
        """Inspect all writable roots for unsafe hardlink aliases."""
        return check_hardlinks(
            self._rw_workspace_roots,
            self._non_delegable_write_paths,
            cache_roots=tuple(m.host_path for m in self._cache_mounts),
            runtime_secret_paths=self._runtime_secret_paths,
        )

    async def run_command(
        self,
        command: str | None = None,
        *,
        argv: list[str] | None = None,
        cwd: str | None = None,
        timeout_seconds: float = 30.0,
        env: dict[str, str] | None = None,
        network: bool = False,
    ) -> CommandResult:
        if sys.platform != "darwin":
            return CommandResult(
                exit_code=None,
                error="macOS Seatbelt is unavailable on this platform",
                sandbox_backend="seatbelt",
            )
        if not os.path.isfile(_SANDBOX_EXECUTABLE) or not os.access(
            _SANDBOX_EXECUTABLE, os.X_OK
        ):
            return CommandResult(
                exit_code=None,
                error="macOS sandbox-exec is unavailable",
                sandbox_backend="seatbelt",
            )
        if (command is None) == (argv is None):
            return CommandResult(
                exit_code=None,
                error="exactly one of command or argv is required",
                sandbox_backend="seatbelt",
            )
        if not isinstance(network, bool):
            return CommandResult(
                exit_code=None,
                error="network parameter must be a boolean",
                sandbox_backend="seatbelt",
            )
        if network and not self._approved_network:
            return CommandResult(
                exit_code=None,
                error="outbound network access requires an approved sandbox_network scope",
                sandbox_backend="seatbelt",
            )
        resolved_cwd, error = self._validate_cwd(cwd)
        if error:
            return CommandResult(
                exit_code=None,
                error=error,
                sandbox_backend="seatbelt",
            )

        hardlink_error = await asyncio.to_thread(
            self._preflight_cache.check,
            self._all_writable_roots,
            self._non_delegable_write_paths,
            walk_fn=self._sensitive_hardlink_error,
        )
        if hardlink_error:
            return CommandResult(
                exit_code=None,
                error=hardlink_error,
                sandbox_backend="seatbelt",
            )

        define_args: list[str] = ["-D", f"WORKSPACE={self._workspace}"]
        for index, extra_root in enumerate(self._extra_roots):
            define_args.extend(["-D", f"EXTRA_ROOT_{index}={extra_root}"])
        for index, ro_root in enumerate(self._share_read_only):
            define_args.extend(["-D", f"SHARE_RO_{index}={ro_root}"])
        for index, mount in enumerate(self._cache_mounts):
            define_args.extend(["-D", f"CACHE_ROOT_{index}={mount.host_path}"])
        for index, runtime_secret_path in enumerate(self._runtime_secret_paths):
            define_args.extend(["-D", f"RUNTIME_SECRET_{index}={runtime_secret_path}"])
        for index, protected_path in enumerate(self._non_delegable_write_paths):
            define_args.extend(
                ["-D", f"NON_DELEGABLE_WRITE_{index}={protected_path}"]
            )

        effective_network = bool(network or self._approved_host_access)
        wrapped_argv = [
            _SANDBOX_EXECUTABLE,
            *define_args,
            "-p",
            self._profile(network=effective_network),
        ]
        if command is not None:
            wrapped_argv.extend(["/bin/sh", "-c", command])
        else:
            assert argv is not None
            wrapped_argv.extend(argv)
        result = await self._host.run_command(
            argv=wrapped_argv,
            cwd=resolved_cwd,
            timeout_seconds=timeout_seconds,
            env=self._safe_environment(env),
        )
        result.sandbox_backend = "seatbelt"
        result.network_used = effective_network
        if not effective_network and result.exit_code not in (None, 0):
            result.network_denied_hint = detect_network_denied_hint(
                result.stdout, result.stderr
            )
        self.last_result = result
        return result
