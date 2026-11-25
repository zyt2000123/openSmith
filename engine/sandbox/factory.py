"""Cross-platform sandbox environment factory."""

from __future__ import annotations

import os
import sys

from .config import ResolvedSandboxProfile
from .host import ExecutionEnvironment
from .linux_bwrap import (
    BwrapAvailability,
    DegradedHostSandboxEnvironment,
    LinuxBwrapEnvironment,
    UnavailableSandboxEnvironment,
    detect_bwrap_availability,
)
from .macos_seatbelt import _SANDBOX_EXECUTABLE, MacOSSeatbeltEnvironment


def _seatbelt_available() -> bool:
    return os.path.isfile(_SANDBOX_EXECUTABLE) and os.access(_SANDBOX_EXECUTABLE, os.X_OK)


def _fallback_environment(
    profile: ResolvedSandboxProfile,
    *,
    code: str,
    reason: str,
    remediation: str,
) -> ExecutionEnvironment:
    if profile.fallback_policy == "host_with_approval":
        return DegradedHostSandboxEnvironment(
            workspace=profile.workspace,
            reason=reason,
            non_delegable_write_paths=profile.non_delegable_write_paths,
            profile=profile,
        )
    return UnavailableSandboxEnvironment(
        reason=reason,
        code=code,
        remediation=remediation,
    )


def create_sandbox_environment(
    profile: ResolvedSandboxProfile,
    *,
    platform: str | None = None,
    bwrap_availability: BwrapAvailability | None = None,
    seatbelt_available: bool | None = None,
) -> ExecutionEnvironment:
    """Create the appropriate OS sandbox backend for ``profile`` (fail-closed)."""
    active_platform = platform or sys.platform
    choice = profile.backend

    if choice == "seatbelt" or (choice == "auto" and active_platform == "darwin"):
        if active_platform != "darwin":
            return _fallback_environment(
                profile,
                code="SEATBELT_UNSUPPORTED_PLATFORM",
                reason=f"macOS Seatbelt backend is not supported on platform '{active_platform}'",
                remediation="Set sandbox.backend: auto or bwrap in ~/.agent-smith/config.yaml",
            )
        sb_ready = _seatbelt_available() if seatbelt_available is None else seatbelt_available
        if not sb_ready:
            return _fallback_environment(
                profile,
                code="SEATBELT_MISSING",
                reason="/usr/bin/sandbox-exec is missing or not executable",
                remediation="Verify macOS system integrity and run `smith doctor`.",
            )
        return MacOSSeatbeltEnvironment(workspace=profile.workspace, profile=profile)

    if choice == "bwrap" or (choice == "auto" and active_platform == "linux"):
        if active_platform != "linux":
            return _fallback_environment(
                profile,
                code="BWRAP_UNSUPPORTED_PLATFORM",
                reason=f"Linux bubblewrap backend is not supported on platform '{active_platform}'",
                remediation="Set sandbox.backend: auto or seatbelt in ~/.agent-smith/config.yaml",
            )
        avail = (
            bwrap_availability
            if bwrap_availability is not None
            else detect_bwrap_availability()
        )
        if not avail.ready:
            return _fallback_environment(
                profile,
                code=avail.code or "BWRAP_UNAVAILABLE",
                reason=avail.reason or "bubblewrap sandbox is unavailable",
                remediation=avail.remediation or "Run `smith doctor` for fix instructions.",
            )
        return LinuxBwrapEnvironment(
            workspace=profile.workspace,
            profile=profile,
            bwrap_binary=avail.binary_path,
        )

    return _fallback_environment(
        profile,
        code="SANDBOX_PLATFORM_UNSUPPORTED",
        reason=f"No native OS sandbox is implemented for platform '{active_platform}'",
        remediation=(
            "Use macOS, Linux, or WSL2 (or explicitly opt into "
            "sandbox.fallback_policy: host_with_approval in ~/.agent-smith/config.yaml)."
        ),
    )


__all__ = ("create_sandbox_environment",)
