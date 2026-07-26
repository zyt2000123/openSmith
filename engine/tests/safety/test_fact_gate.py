from __future__ import annotations

from engine.safety.fact_gate import (
    FactGate,
    FactGateContext,
    _is_read_only_sed,
    current_fact_gate,
    use_fact_gate,
)
from engine.safety.tool_guard import GuardResult, PermissionLevel
from engine.safety.tool_policy import ToolPolicy
from engine.tool.interface import ToolCall, ToolDefinition


def _call(name: str, **arguments: object) -> ToolCall:
    return ToolCall(id="tool-1", name=name, arguments=dict(arguments))


def _gate(*, turn_id: str = "turn-1", enabled: bool = True) -> FactGate:
    return FactGate(
        FactGateContext(
            session_id="session-1",
            turn_id=turn_id,
        ),
        enabled=enabled,
    )


def test_first_file_write_is_challenged_and_retry_is_allowed() -> None:
    gate = _gate()
    call = _call("edit_file", path="engine/example.py")

    first = gate.evaluate(call)
    same_round = gate.evaluate(call)
    gate.begin_round()
    retry = gate.evaluate(call)

    assert first.challenged
    assert "ALL files that import" in first.reason
    assert "public functions/classes" in first.reason
    assert "data schema" in first.reason
    assert "current instruction verbatim" in first.reason
    assert same_round.challenged
    assert not retry.challenged


def test_file_gate_resets_for_a_new_turn() -> None:
    call = _call("write_file", path="engine/new_module.py")
    assert _gate(turn_id="turn-1").evaluate(call).challenged
    assert _gate(turn_id="turn-2").evaluate(call).challenged


def test_read_only_shell_introspection_is_allowed() -> None:
    gate = _gate()

    assert not gate.evaluate(_call("shell", command="git status --short")).challenged
    assert not gate.evaluate(_call("shell", command="rg -n 'ToolGuard' engine")).challenged
    assert not gate.evaluate(_call("shell", command="pwd && git diff --stat")).challenged
    assert not gate.evaluate(_call("shell", command="rg 'a|b' engine")).challenged


def test_shell_allowlist_rejects_mutation_hidden_in_composition() -> None:
    commands = [
        "git status && mkdir build",
        "git diff --stat > report.txt",
        "find . -exec rm {} \\;",
        "echo \"$(rm -rf build)\"",
        "git branch -D old-branch",
        "git branch new-branch",
        "pwd\nmkdir build",
        "pwd & mkdir build",
        "sed -i.bak 's/a/b/' file.txt",
        "find . -fprint report.txt",
        "find . -fprintf report.txt '%p\\n'",
        "sort -o report.txt input.txt",
        "sort -oreport.txt input.txt",
        "sort --compress-program='sh -c touch /tmp/pwned' input.txt",
        "tree -o report.txt .",
        "tree --output=report.txt .",
        "uniq input.txt output.txt",
        "git diff --output=report.patch",
        "git -c alias.status='!touch /tmp/pwned' status",
        "git --exec-path=/tmp status",
        "git branch --list --delete old-branch",
        "git diff --ext-diff",
        "git show --textconv HEAD:file.txt",
        "git grep --open-files-in-pager='sh -c touch /tmp/pwned' needle",
        "git grep -O'sh -c touch /tmp/pwned' needle",
        "git --paginate status",
        "sed -n 'w report.txt' input.txt",
        "date --set='2030-01-01'",
        "/tmp/ls",
        "PATH=/tmp:$PATH ls",
    ]

    for index, command in enumerate(commands):
        gate = _gate(turn_id=f"turn-{index}")
        assert gate.evaluate(_call("shell", command=command)).challenged, command


def test_first_state_changing_shell_is_challenged_once_per_turn() -> None:
    gate = _gate()

    first = gate.evaluate(_call("shell", command="mkdir -p build"))
    same_round = gate.evaluate(_call("shell", command="npm test"))
    gate.begin_round()
    retry = gate.evaluate(_call("shell", command="npm test"))

    assert first.challenged
    assert "changes or produces" in first.reason
    assert "rollback" in first.reason
    assert same_round.challenged
    assert not retry.challenged


def test_is_read_only_sed_handles_e_flag_and_write_commands() -> None:
    # Plain print is read-only
    assert _is_read_only_sed(["-n", "1p", "file"])
    # -e flag is filtered, not mistaken for the script
    assert _is_read_only_sed(["-n", "-e", "1p", "file"])
    # ;-separated print sub-commands remain read-only
    assert _is_read_only_sed(["-n", "1p;2p", "file"])
    # A write sub-command hidden after a print is not read-only
    assert not _is_read_only_sed(["-n", "1p;w /tmp/evil", "file"])
    # A direct write command is not read-only
    assert not _is_read_only_sed(["-n", "w report.txt", "input.txt"])


def test_gate_can_be_disabled() -> None:
    gate = _gate(enabled=False)

    assert not gate.evaluate(_call("edit_file", path="engine/example.py")).challenged
    assert not gate.evaluate(_call("shell", command="mkdir build")).challenged


def test_structured_state_tools_gate_mutations_but_allow_reads() -> None:
    read_calls = [
        _call("git_ops", action="status"),
        _call("skill_manage", action="get", agent_id="smith", skill_name="tdd"),
        _call("memory_ops", action="search", agent_id="smith", query="preferences"),
    ]
    for index, call in enumerate(read_calls):
        assert not _gate(turn_id=f"read-{index}").evaluate(call).challenged

    mutation_calls = [
        _call("git_ops", action="commit", message="change"),
        _call("skill_manage", action="edit", agent_id="smith", skill_name="tdd"),
        _call("memory_ops", action="remove", agent_id="smith", memory_id="old"),
    ]
    for index, call in enumerate(mutation_calls):
        gate = _gate(turn_id=f"write-{index}")
        assert gate.evaluate(call).challenged
        assert gate.evaluate(call).challenged
        gate.begin_round()
        assert not gate.evaluate(call).challenged


def test_metadata_read_actions_gate_unlisted_actions() -> None:
    # notes_ops is absent from the hardcoded structured-tool table — the gate
    # must act purely on the declared ToolDefinition metadata.
    registry = {
        "notes_ops": ToolDefinition(
            name="notes_ops",
            description="",
            read_actions=frozenset({"get", "list"}),
        )
    }
    gate = FactGate(
        FactGateContext(session_id="session-1", turn_id="turn-meta"),
        enabled=True,
        tool_registry=registry,
    )

    assert not gate.evaluate(_call("notes_ops", action="list")).challenged

    challenge = gate.evaluate(_call("notes_ops", action="delete"))
    assert challenge.challenged
    assert "notes_ops.delete" in challenge.reason


def test_metadata_write_tool_gets_file_challenge() -> None:
    registry = {
        "doc_writer": ToolDefinition(
            name="doc_writer",
            description="",
            path_args=("target",),
            is_write_tool=True,
        )
    }
    gate = FactGate(
        FactGateContext(session_id="session-1", turn_id="turn-meta-write"),
        enabled=True,
        tool_registry=registry,
    )
    call = _call("doc_writer", target="engine/example.py")

    first = gate.evaluate(call)
    gate.begin_round()
    retry = gate.evaluate(call)

    assert first.challenged
    assert "ALL files that import" in first.reason
    assert not retry.challenged


def test_request_context_binding_is_restored() -> None:
    gate = _gate()
    assert current_fact_gate() is None

    with use_fact_gate(gate):
        assert current_fact_gate() is gate

    assert current_fact_gate() is None


def test_request_context_binding_is_isolated_between_async_tasks() -> None:
    import asyncio

    async def run() -> tuple[FactGate | None, FactGate | None]:
        first = _gate(turn_id="turn-a")
        second = _gate(turn_id="turn-b")
        ready_a = asyncio.Event()
        ready_b = asyncio.Event()
        release = asyncio.Event()

        async def observe(gate: FactGate, ready: asyncio.Event) -> FactGate | None:
            with use_fact_gate(gate):
                ready.set()
                await release.wait()
                return current_fact_gate()

        task_a = asyncio.create_task(observe(first, ready_a))
        task_b = asyncio.create_task(observe(second, ready_b))
        await ready_a.wait()
        await ready_b.wait()
        release.set()
        return await task_a, await task_b

    observed_a, observed_b = asyncio.run(run())
    assert observed_a is not None and observed_a.context.turn_id == "turn-a"
    assert observed_b is not None and observed_b.context.turn_id == "turn-b"
    assert current_fact_gate() is None


class _HardBlockingGuard:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    def check(self, call: ToolCall) -> GuardResult:
        self.calls.append(call)
        return GuardResult(
            allowed=False,
            reason="hard safety block",
            level=PermissionLevel.DESTRUCTIVE,
        )


def test_tool_policy_applies_hard_guard_before_fact_gate() -> None:
    gate = _gate()
    guard = _HardBlockingGuard()
    call = _call("edit_file", path="engine/example.py")

    decision = ToolPolicy(guard, fact_gate=gate).evaluate(call)

    assert guard.calls == [call]
    assert not decision.allowed
    assert not decision.challenged
    assert decision.observation == "[BLOCKED] hard safety block"
    # The hard block must not consume the first-touch fact challenge.
    assert gate.evaluate(call).challenged


def test_tool_policy_maps_fact_gate_to_preflight_observation() -> None:
    decision = ToolPolicy(fact_gate=_gate()).evaluate(
        _call("write_file", path="engine/new_module.py")
    )

    assert not decision.allowed
    assert decision.challenged
    assert decision.observation.startswith("[PREFLIGHT]")
