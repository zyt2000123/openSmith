"""Test auto-whitelist functionality for approved path accesses."""

from pathlib import Path
import pytest

from engine.tool.interface import ToolCall
from engine.tool.registry import ToolRegistry
from engine.safety.tool_guard import ToolGuard
from engine.safety.approval import ApprovalScope


@pytest.mark.asyncio
async def test_approved_path_adds_directory_to_whitelist(tmp_path):
    """After approving access to a file, the parent directory is auto-whitelisted."""

    # Setup
    working_dir = tmp_path / "project"
    working_dir.mkdir()
    external_dir = tmp_path / "documents"
    external_dir.mkdir()
    external_file = external_dir / "notes.txt"
    external_file.write_text("test content")

    rules_path = tmp_path / "rules.json"
    rules_path.write_text("[]")

    guard = ToolGuard(rules_path, allowed_dirs=None)
    guard.set_working_directory(working_dir)

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

    # Initial state: external_dir NOT in whitelist
    assert not guard.whitelist.is_path_allowed(str(external_dir))

    # Create and authorize a call
    call = ToolCall(
        id="call1",
        name="read_file",
        arguments={"path": str(external_file)},
    )

    # Check what the guard returns
    decision = guard.check(call)
    print(f"Decision: allowed={decision.allowed}, approval_required={decision.approval_required}, boundary_block={decision.boundary_block}")
    print(f"Approval scope: {decision.approval_scope}")

    approval_scope = ApprovalScope.path(str(external_file), writing=False)
    print(f"Created approval_scope: kind={approval_scope.kind}, target={approval_scope.target}")

    # Execute with authorization (this should trigger auto-whitelist)
    with registry.authorize_execution(call, approval_id="test-approval", approval_scope=approval_scope):
        result = await registry.execute(call)

    print(f"Whitelist after execution: {guard.whitelist._allowed_paths}")
    print(f"Result: {result}")

    # After execution: external_dir SHOULD be in whitelist
    assert guard.whitelist.is_path_allowed(str(external_dir)), \
        f"Parent directory {external_dir} should be auto-whitelisted"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
