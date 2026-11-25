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
from .host import MAX_OUTPUT, CommandResult, ExecutionEnvironment, LocalExecutionEnvironment
from .macos_seatbelt import MacOSSeatbeltEnvironment
from .preflight import HardlinkPreflightCache

__all__ = (
    "CacheMount",
    "CommandResult",
    "DefaultNetworkMode",
    "ExecutionEnvironment",
    "HardlinkPreflightCache",
    "LocalExecutionEnvironment",
    "MacOSSeatbeltEnvironment",
    "MAX_OUTPUT",
    "ResolvedSandboxProfile",
    "SandboxBackendChoice",
    "SandboxFallbackPolicy",
    "SandboxProfileConfig",
    "resolve_sandbox_profile",
)
