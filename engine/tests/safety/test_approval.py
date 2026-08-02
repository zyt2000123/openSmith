from __future__ import annotations

import asyncio

from engine.safety.approval import (
    ApprovalBroker,
    ApprovalRequest,
    ApprovalScope,
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


def test_approval_summary_redacts_secret_flag_pairs_in_list_arguments() -> None:
    summary = summarize_arguments({
        "args": ["--token", "sk-test-123456", "--model", "gpt-4o"],
        "extra": ["--password=supersecret", "--plain", "visible"],
        "mixed": ["--client-secret", "s3cr3t", "--region", "us-east-1"],
    })

    assert summary["args"] == ["--token", "***", "--model", "gpt-4o"]
    assert summary["extra"] == ["--password=***", "--plain", "visible"]
    assert summary["mixed"] == ["--client-secret", "***", "--region", "us-east-1"]


def test_approval_summary_redacts_secret_shaped_list_values() -> None:
    summary = summarize_arguments({
        "args": [
            "ghp_abcdefghijklmnopqrstuvwx",
            "AKIA0123456789ABCDEF",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
            "Bearer tok_abc123",
        ],
    })

    assert summary["args"] == ["***", "***", "***", "***"]


def test_approval_summary_redacts_case_variant_secret_flags() -> None:
    summary = summarize_arguments({
        "args": ["--TOKEN", "sk-abcdefgh", "--Password", "hunter2"],
    })

    assert summary["args"] == ["--TOKEN", "***", "--Password", "***"]


def test_approval_summary_redacts_secrets_embedded_in_string_arguments() -> None:
    """A key inside a shell command must not survive into the summary."""
    summary = summarize_arguments({
        "command": (
            "curl -H 'Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz123456' "
            "https://api.example.com"
        ),
        "command2": "aws configure --secret-access-key AKIA0123456789ABCDEF --region us",
    })

    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in summary["command"]
    assert summary["command"] == (
        "curl -H 'Authorization: Bearer ***' https://api.example.com"
    )
    assert "AKIA0123456789ABCDEF" not in summary["command2"]
    assert summary["command2"] == "aws configure --secret-access-key *** --region us"


def test_approval_summary_redacts_short_bearer_and_gitlab_tokens() -> None:
    """Short Bearer tokens and GitLab PATs embedded in commands must redact
    even though they are not whole-string credentials or recognized key
    families."""
    summary = summarize_arguments({
        "cmd1": "curl -H 'Authorization: Bearer abc123xyz' https://api.example.com",
        "cmd2": "curl -H 'PRIVATE-TOKEN: glpat-abcdefghijklmnopqrstuv' https://gitlab.example.com",
        "cmd3": "git push https://user:pw1234567890@example.com/repo.git",
    })

    assert "abc123xyz" not in summary["cmd1"]
    assert "glpat-abcdefghijklmnopqrstuv" not in summary["cmd2"]
    assert "pw1234567890" not in summary["cmd3"]
    assert "https://api.example.com" in summary["cmd1"]
    assert "https://gitlab.example.com" in summary["cmd2"]
    assert "example.com/repo.git" in summary["cmd3"]


def test_approval_summary_redacts_url_credentials() -> None:
    summary = summarize_arguments({
        "url": "https://user:supersecret@example.com/data",
    })

    assert "supersecret" not in summary["url"]
    assert summary["url"] == "https://***@example.com/data"


def test_approval_summary_preserves_ordinary_content() -> None:
    """The embedded-secret scan must not mangle everyday arguments."""
    summary = summarize_arguments({
        "command": "npm install --save-dev eslint",
        "url": "https://example.com/api?key=normal&limit=10",
        "path": "/Users/me/sk-devtools/readme.md",
        "message": "fix: handle token refresh",
    })

    assert summary["command"] == "npm install --save-dev eslint"
    assert summary["url"] == "https://example.com/api?key=normal&limit=10"
    assert summary["path"] == "/Users/me/sk-devtools/readme.md"
    assert summary["message"] == "fix: handle token refresh"


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


def test_approval_scope_is_visible_and_describes_one_host_command() -> None:
    scope = ApprovalScope.host_command("cat ~/Downloads/report.txt")
    request = ApprovalRequest(
        approval_id="approval-scope",
        run_id="run-1",
        tool_name="shell",
        level="execute",
        reason="Host access requires approval",
        arguments_summary={"command": "cat ~/Downloads/report.txt"},
        scope=scope,
    )
    presentation = build_approval_presentation(
        "shell",
        "execute",
        request.reason,
        request.arguments_summary,
        scope=scope,
    )

    assert request.to_dict()["scope"] == {
        "kind": "host_command",
        "target": "cat ~/Downloads/report.txt",
        "access": ["filesystem", "network", "process"],
        "high_risk": False,
    }
    assert presentation.details[-1].to_dict() == {
        "label": "Access scope",
        "value": "Host filesystem, network, and process access for this exact command",
    }
