from __future__ import annotations

import asyncio

from engine.safety.approval import (
    ApprovalBroker,
    ApprovalRequest,
    ApprovalTimeoutError,
    build_approval_presentation,
    summarize_arguments,
)


def test_approval_summary_redacts_nested_secrets_and_terminal_controls() -> None:
    summary = summarize_arguments({
        "command": "printf '\x1b[31msecret\x1b[0m'",
        "nested": {"api_key": "do-not-show", "items": [{"token": "also-secret"}]},
    })

    assert "\x1b" not in summary["command"]
    assert summary["nested"]["api_key"] == "***"
    assert summary["nested"]["items"] == [{"token": "***"}]


def test_approval_broker_wakes_the_waiting_run_with_the_user_decision() -> None:
    async def run() -> bool:
        broker = ApprovalBroker()
        request = broker.open(
            ApprovalRequest(
                approval_id="approval-1",
                run_id="run-1",
                tool_name="shell",
                level="execute",
                reason="Approval required for shell",
                arguments_summary={"command": "git status"},
            )
        )
        waiter = asyncio.create_task(broker.wait(request))
        await asyncio.sleep(0)

        assert broker.is_pending("run-1", "approval-1")
        assert broker.resolve("run-1", "approval-1", True)
        assert not broker.resolve("run-1", "approval-1", False)
        assert await waiter
        assert not broker.is_pending("run-1", "approval-1")
        return True

    assert asyncio.run(run())


def test_approval_broker_times_out_and_clears_pending() -> None:
    async def run() -> None:
        broker = ApprovalBroker()
        request = broker.open(
            ApprovalRequest(
                approval_id="approval-timeout",
                run_id="run-1",
                tool_name="shell",
                level="execute",
                reason="Approval required",
                arguments_summary={},
            )
        )
        try:
            await broker.wait(request, timeout_seconds=0.01)
        except ApprovalTimeoutError:
            pass
        else:
            raise AssertionError("approval wait should time out")

        assert not broker.resolve("run-1", "approval-timeout", True)

    asyncio.run(run())


def test_approval_presentation_describes_file_and_git_actions() -> None:
    write = build_approval_presentation(
        "write_file",
        "write",
        "Approval required for write_file",
        {"path": "/workspace/notes.md", "content": "hello", "append": False},
    )
    assert write.to_dict() == {
        "title": "Write a file",
        "summary": "Write to /workspace/notes.md",
        "details": [
            {"label": "Path", "value": "/workspace/notes.md"},
            {"label": "Append", "value": "false"},
            {"label": "Content preview", "value": "hello"},
        ],
        "reason": "This will change file contents.",
    }

    git = build_approval_presentation(
        "git_ops",
        "write",
        "Approval required for git_ops",
        {"action": "commit", "cwd": "/workspace/project", "message": "fix approval"},
    )
    assert git.title == "Commit Git changes"
    assert git.summary == "Create a Git commit"
    assert [detail.to_dict() for detail in git.details] == [
        {"label": "Action", "value": "commit"},
        {"label": "Working directory", "value": "/workspace/project"},
        {"label": "Commit message", "value": "fix approval"},
    ]


def test_approval_presentation_uses_custom_tool_description_as_fallback() -> None:
    presentation = build_approval_presentation(
        "mcp_deploy",
        "execute",
        "Approval required for mcp_deploy",
        {"environment": "staging"},
        tool_description="Deploy the current project to an environment.",
    )

    assert presentation.title == "Use Mcp deploy"
    assert presentation.summary == "Deploy the current project to an environment."
    assert presentation.details[0].to_dict() == {"label": "Environment", "value": "staging"}
