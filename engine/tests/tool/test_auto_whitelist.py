"""Test auto-whitelist functionality for approved path accesses."""

from pathlib import Path
import pytest

from engine.tool.interface import ToolCall
from engine.tool.registry import ToolRegistry
from engine.safety.tool_guard import ToolGuard
from engine.safety.approval import ApprovalScope


def _registry_with_read_file(guard: ToolGuard) -> ToolRegistry:
    registry = ToolRegistry()
    registry.bind_tool_guard(guard)

    def read_file_impl(path: str) -> str:
        return Path(path).read_text()

    registry.register(
        "read_file",
        "Read a file",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        read_file_impl,
        path_args=("path",),
        is_write_tool=False,
    )
    return registry


@pytest.mark.asyncio
async def test_external_approval_whitelists_only_the_exact_file(tmp_path):
    """One external approval must not grant the whole directory.

    Directory granularity let a single approved read (e.g. /etc/hosts) silently
    grant every sibling file in an arbitrary system directory, including
    credential-bearing names the name-based sensitive checks do not cover.
    """
    working_dir = tmp_path / "project"
    working_dir.mkdir()
    external_dir = tmp_path / "documents"
    external_dir.mkdir()
    external_file = external_dir / "notes.txt"
    external_file.write_text("test content")
    (external_dir / "master.passwd").write_text("root:hashed:0:0:root:/root:/bin/sh")

    rules_path = tmp_path / "rules.json"
    rules_path.write_text("[]")

    guard = ToolGuard(rules_path, allowed_dirs=None)
    guard.set_working_directory(working_dir)
    registry = _registry_with_read_file(guard)

    # Initial state: external dir/file NOT whitelisted.
    assert not guard.whitelist.is_path_allowed(str(external_dir))
    assert not guard.whitelist.is_path_allowed(str(external_file))

    call = ToolCall(
        id="call1",
        name="read_file",
        arguments={"path": str(external_file)},
    )
    decision = guard.check(call)
    assert decision.boundary_block, "external path must be a boundary approval"

    with registry.authorize_execution(
        call,
        approval_id="test-approval",
        approval_scope=decision.approval_scope,
    ):
        result = await registry.execute(call)
    assert "test content" in result.content

    # The exact file is whitelisted; the parent directory is NOT.
    assert guard.whitelist.is_path_allowed(str(external_file))
    assert not guard.whitelist.is_path_allowed(str(external_dir))

    # Sibling read is still blocked: no silent directory-wide grant.
    sibling = ToolCall(
        id="call2",
        name="read_file",
        arguments={"path": str(external_dir / "master.passwd")},
    )
    blocked = await registry.execute(sibling)
    assert blocked.is_error
    assert "[BLOCKED]" in blocked.content


@pytest.mark.asyncio
async def test_high_risk_approval_is_never_whitelist_cached(tmp_path):
    """High-risk approvals must be re-granted every time.

    A sensitive file (``.env``) inside the workspace still requires approval,
    and approving it must NOT populate the session whitelist — only elevated
    boundary approvals are cacheable.  The whitelist never broadened the
    boundary for high-risk paths (they are not boundary blocks), but the
    stricter tier gate makes that guarantee explicit.
    """
    working_dir = tmp_path / "project"
    working_dir.mkdir()
    sub = working_dir / "sub"
    sub.mkdir()
    env_file = sub / ".env"
    env_file.write_text("SECRET=1")

    rules_path = tmp_path / "rules.json"
    rules_path.write_text("[]")

    guard = ToolGuard(rules_path, allowed_dirs=[working_dir])
    registry = _registry_with_read_file(guard)

    # A sensitive file inside the workspace still requires approval...
    call = ToolCall(id="c1", name="read_file", arguments={"path": str(env_file)})
    decision = guard.check(call)
    assert decision.approval_required and not decision.boundary_block
    assert decision.risk.value == "high"
    with registry.authorize_execution(
        call, approval_id="a", approval_scope=decision.approval_scope
    ):
        await registry.execute(call)

    # ...but the parent directory is NOT whitelisted: a high-risk approval
    # must be re-granted on the next attempt regardless of location.
    assert not guard.whitelist.is_path_allowed(str(sub))
    assert not guard.whitelist.is_path_allowed(str(env_file))

