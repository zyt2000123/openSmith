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

__all__ = (
    "BwrapAvailability",
    "CacheMount",
    "CommandResult",
    "DefaultNetworkMode",
    "DegradedHostSandboxEnvironment",
    "ExecutionEnvironment",
    "HardlinkPreflightCache",
    "LinuxBwrapEnvironment",
    "LocalExecutionEnvironment",
    "MacOSSeatbeltEnvironment",
    "MAX_OUTPUT",
    "ResolvedSandboxProfile",
    "SandboxBackendChoice",
    "SandboxFallbackPolicy",
    "SandboxProfileConfig",
    "UnavailableSandboxEnvironment",
    "create_sandbox_environment",
    "detect_bwrap_availability",
    "detect_network_denied_hint",
    "probe_bwrap_availability",
    "resolve_sandbox_profile",
)
