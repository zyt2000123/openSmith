"""Cross-platform execution sandbox contracts and backends.

Only the macOS-native Seatbelt backend is implemented today.  Tool code uses
the ``ExecutionEnvironment`` contract and does not depend on platform details.
"""

from .config import (
    CacheMount,
    DefaultNetworkMode,
    ResolvedSandboxProfile,
    SandboxBackendChoice,
    SandboxFallbackPolicy,
    SandboxProfileConfig,
    resolve_sandbox_profile,
)
from .factory import create_sandbox_environment
from .host import (
    MAX_OUTPUT,
    CommandResult,
    ExecutionEnvironment,
    LocalExecutionEnvironment,
    detect_network_denied_hint,
)
from .linux_bwrap import (
    BwrapAvailability,
    DegradedHostSandboxEnvironment,
    LinuxBwrapEnvironment,
    UnavailableSandboxEnvironment,
    detect_bwrap_availability,
    probe_bwrap_availability,
)
from .macos_seatbelt import MacOSSeatbeltEnvironment
from .preflight import HardlinkPreflightCache

_DOCTOR_EXPORTS = frozenset({
    "CheckStatus",
    "DoctorCheck",
    "DoctorReport",
    "SandboxHealthReport",
    "check_sandbox_health",
    "format_doctor_report",
    "run_doctor",
    "run_doctor_async",
})


def __getattr__(name: str):
    if name in _DOCTOR_EXPORTS:
        from . import doctor

        return getattr(doctor, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = (
    "BwrapAvailability",
    "CacheMount",
    "CheckStatus",
    "CommandResult",
    "DefaultNetworkMode",
    "DegradedHostSandboxEnvironment",
    "DoctorCheck",
    "DoctorReport",
    "ExecutionEnvironment",
    "HardlinkPreflightCache",
    "LinuxBwrapEnvironment",
    "LocalExecutionEnvironment",
    "MacOSSeatbeltEnvironment",
    "MAX_OUTPUT",
    "ResolvedSandboxProfile",
    "SandboxBackendChoice",
    "SandboxFallbackPolicy",
    "SandboxHealthReport",
    "SandboxProfileConfig",
    "UnavailableSandboxEnvironment",
    "check_sandbox_health",
    "create_sandbox_environment",
    "detect_bwrap_availability",
    "detect_network_denied_hint",
    "format_doctor_report",
    "probe_bwrap_availability",
    "resolve_sandbox_profile",
    "run_doctor",
    "run_doctor_async",
)
