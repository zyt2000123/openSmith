from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


ToolSideEffect = Literal["none", "write", "external", "destructive"]
ToolConcurrency = Literal["safe", "serial"]
ToolExecutionEnvironment = Literal["host", "sandbox", "either"]
ToolApprovalPolicy = Literal["never", "policy", "always"]


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict = field(default_factory=dict)
    # Runtime infrastructure tools can be registered but kept out of the
    # model-visible default tool set. Visibility is metadata, never a caller
    # maintained list of tool names.
    hidden: bool = False
    # An opaque command string needs path extraction and explicit approval;
    # shell is currently the only provider that declares this capability.
    opaque_command: bool = False
    # A provider that opens network connections. Network capability is always
    # approved even when the operation otherwise looks read-only.
    network_access: bool = False
    # Security metadata — the runtime policy resolves permissions and approval
    # from these declarations. Path-policy compatibility fallbacks remain in
    # the guard only for legacy direct callers that have not bound a registry.
    path_args: tuple[str, ...] = ()
    list_path_args: tuple[str, ...] = ()
    is_write_tool: bool = False
    permission_level: str = ""
    approval_policy: ToolApprovalPolicy = "never"
    read_actions: frozenset[str] = field(default_factory=frozenset)
    # Rich execution contract. Legacy providers keep the safe defaults.
    timeout_seconds: float | None = None
    retryable: bool = False
    side_effect: ToolSideEffect = "none"
    idempotent: bool = False
    concurrency: ToolConcurrency = "safe"
    execution_environment: ToolExecutionEnvironment = "host"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict = field(default_factory=dict)
    idempotency_key: str | None = None


@dataclass
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False
    error_kind: str | None = None
    retryable: bool = False
    timed_out: bool = False
    side_effect_status: Literal["none", "completed", "unknown"] = "none"
    metadata: dict[str, object] = field(default_factory=dict)
