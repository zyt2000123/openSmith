"""Sub-agent fan-out: isolation, concurrency, failure containment, recursion guard.

The sub-agent contract is that the parent pays for a *summary*, not a
transcript, and that one delegated task cannot take the batch — or the parent
run — down with it. These tests hold both ends of that.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from engine.execution.subagent import (
    SUB_AGENT_TOOL_NAME,
    SubAgentCatalog,
    SubAgentCatalogError,
    SubAgentTask,
    run_sub_agents,
)
from engine.execution.subagent.catalog import MAX_DECLARED_ITERS
from engine.execution.subagent.runner import MAX_TASKS_PER_CALL
from engine.llm.client import ChatResponse
from engine.llm.contracts import ToolCallData
from engine.tool.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[3]
SHIPPED_SUBAGENTS = ROOT / "agents" / "subagents"


def _write_type(directory: Path, spec_id: str, **overrides: object) -> Path:
    fields = {
        "schema": "agentsmith.subagent/v1",
        "id": spec_id,
        "name": spec_id.title(),
        "description": f"{spec_id} description",
        "prompt": f"You are {spec_id}.",
        "tools": ["echo_tool"],
        **overrides,
    }
    lines = []
    for key, value in fields.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"  - {item}" for item in value)
        else:
            lines.append(f"{key}: {value}")
    path = directory / f"{spec_id}.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    def echo_tool(text: str) -> str:
        return f"echo:{text}"

    registry.register(
        "echo_tool",
        "Echo a string",
        {"properties": {"text": {"type": "string"}}, "required": ["text"]},
        echo_tool,
        permission_level="read",
        approval_policy="never",
        side_effect="none",
    )
    # Registered so a recursion attempt has something real to reach for; the
    # scoping is what must keep it out of a sub-agent's hands.
    registry.register(
        SUB_AGENT_TOOL_NAME,
        "Spawn sub-agents",
        {"properties": {}},
        lambda: "spawned",
        permission_level="read",
        approval_policy="never",
        side_effect="none",
    )
    return registry


class _ReplyLLM:
    """Answers every turn with one fixed final text."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    async def chat(self, messages, tools=None, prefix_cache_key=None):
        self.calls += 1
        return ChatResponse(text=self.text)


class _EchoPromptLLM:
    """Replies with the user prompt, proving each sub-agent got its own brief."""

    async def chat(self, messages, tools=None, prefix_cache_key=None):
        user = next(m for m in messages if m.get("role") == "user")
        return ChatResponse(text=f"report for {user['content']}")


class _OverlapLLM:
    """Records how many sub-agents were inside a provider call at once."""

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.active = 0
        self.peak = 0

    async def chat(self, messages, tools=None, prefix_cache_key=None):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            return ChatResponse(text="done")
        finally:
            self.active -= 1


class _ExplodingLLM:
    """Fails only for the task whose prompt contains ``boom``."""

    async def chat(self, messages, tools=None, prefix_cache_key=None):
        user = next(m for m in messages if m.get("role") == "user")
        if "boom" in user["content"]:
            raise RuntimeError("provider exploded")
        return ChatResponse(text="fine")


class _RecursingLLM:
    """First turn tries to spawn sub-agents, then answers with what happened."""

    def __init__(self) -> None:
        self.calls = 0
        self.tool_reply = ""

    async def chat(self, messages, tools=None, prefix_cache_key=None):
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(
                tool_calls=[ToolCallData(id="c1", name=SUB_AGENT_TOOL_NAME, arguments={})]
            )
        self.tool_reply = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "tool"), ""
        )
        return ChatResponse(text="finished")


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


def test_shipped_catalog_loads_and_declares_no_recursive_tool() -> None:
    catalog = SubAgentCatalog.load(SHIPPED_SUBAGENTS)
    assert catalog, "shipped sub-agent types must load"
    assert set(catalog.ids()) == {"explorer", "reviewer", "implementer"}
    for spec in catalog.specs:
        assert SUB_AGENT_TOOL_NAME not in spec.tools
        assert spec.tools, f"{spec.id} must declare at least one tool"
    assert "explorer:" in catalog.describe()


def test_missing_directory_is_an_empty_catalog_not_an_error(tmp_path: Path) -> None:
    catalog = SubAgentCatalog.load(tmp_path / "absent")
    assert not catalog
    assert catalog.ids() == ()


def test_declared_recursion_is_stripped_and_iters_are_capped(tmp_path: Path) -> None:
    _write_type(
        tmp_path,
        "greedy",
        tools=["echo_tool", SUB_AGENT_TOOL_NAME],
        max_iters=500,
    )
    spec = SubAgentCatalog.load(tmp_path).get("greedy")
    assert spec.tools == ("echo_tool",)
    assert spec.max_iters == MAX_DECLARED_ITERS


def test_malformed_type_fails_loudly(tmp_path: Path) -> None:
    (tmp_path / "broken.yaml").write_text("schema: wrong/v9\nid: broken\n", encoding="utf-8")
    with pytest.raises(SubAgentCatalogError):
        SubAgentCatalog.load(tmp_path)


def test_type_without_tools_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "toolless.yaml").write_text(
        "schema: agentsmith.subagent/v1\nid: toolless\nname: T\n"
        "description: d\nprompt: p\ntools: []\n",
        encoding="utf-8",
    )
    with pytest.raises(SubAgentCatalogError):
        SubAgentCatalog.load(tmp_path)


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_task_gets_an_isolated_conversation(tmp_path: Path) -> None:
    _write_type(tmp_path, "worker")
    catalog = SubAgentCatalog.load(tmp_path)

    outcomes = await run_sub_agents(
        [
            SubAgentTask("worker", "first task", label="one"),
            SubAgentTask("worker", "second task", label="two"),
        ],
        catalog=catalog,
        llm=_EchoPromptLLM(),
        tool_registry=_registry(),
    )

    assert [o.label for o in outcomes] == ["one", "two"]
    assert all(o.ok for o in outcomes)
    # Order preserved, and neither sub-agent saw the other's brief.
    assert outcomes[0].summary == "report for first task"
    assert outcomes[1].summary == "report for second task"


@pytest.mark.asyncio
async def test_fan_out_actually_runs_in_parallel(tmp_path: Path) -> None:
    _write_type(tmp_path, "worker")
    catalog = SubAgentCatalog.load(tmp_path)
    llm = _OverlapLLM()

    outcomes = await run_sub_agents(
        [SubAgentTask("worker", f"task {i}") for i in range(4)],
        catalog=catalog,
        llm=llm,
        tool_registry=_registry(),
        max_parallel=4,
    )

    assert all(o.ok for o in outcomes)
    assert llm.peak > 1, "sub-agents must overlap, not run one after another"


@pytest.mark.asyncio
async def test_max_parallel_is_respected(tmp_path: Path) -> None:
    _write_type(tmp_path, "worker")
    catalog = SubAgentCatalog.load(tmp_path)
    llm = _OverlapLLM()

    await run_sub_agents(
        [SubAgentTask("worker", f"task {i}") for i in range(6)],
        catalog=catalog,
        llm=llm,
        tool_registry=_registry(),
        max_parallel=2,
    )

    assert llm.peak <= 2


@pytest.mark.asyncio
async def test_one_failure_does_not_sink_the_batch(tmp_path: Path) -> None:
    _write_type(tmp_path, "worker")
    catalog = SubAgentCatalog.load(tmp_path)

    outcomes = await run_sub_agents(
        [
            SubAgentTask("worker", "ok task", label="good"),
            SubAgentTask("worker", "boom task", label="bad"),
            SubAgentTask("worker", "another ok", label="also-good"),
        ],
        catalog=catalog,
        llm=_ExplodingLLM(),
        tool_registry=_registry(),
    )

    by_label = {o.label: o for o in outcomes}
    assert by_label["good"].ok and by_label["also-good"].ok
    assert not by_label["bad"].ok
    assert by_label["bad"].error


@pytest.mark.asyncio
async def test_unknown_agent_type_is_a_failed_outcome_not_a_raise(tmp_path: Path) -> None:
    _write_type(tmp_path, "worker")
    catalog = SubAgentCatalog.load(tmp_path)

    outcomes = await run_sub_agents(
        [SubAgentTask("nosuchtype", "go")],
        catalog=catalog,
        llm=_ReplyLLM("unused"),
        tool_registry=_registry(),
    )

    assert not outcomes[0].ok
    assert "nosuchtype" in outcomes[0].error
    assert "worker" in outcomes[0].error  # tells the model what IS available


@pytest.mark.asyncio
async def test_a_sub_agent_cannot_spawn_sub_agents(tmp_path: Path) -> None:
    # Declared with the spawn tool; the catalog strips it and the scoped
    # registry must refuse the call even though the parent registry has it.
    _write_type(tmp_path, "worker", tools=["echo_tool", SUB_AGENT_TOOL_NAME])
    catalog = SubAgentCatalog.load(tmp_path)
    llm = _RecursingLLM()

    outcomes = await run_sub_agents(
        [SubAgentTask("worker", "try to recurse")],
        catalog=catalog,
        llm=llm,
        tool_registry=_registry(),
    )

    assert outcomes[0].ok
    assert "disabled" in llm.tool_reply.lower() or "not available" in llm.tool_reply.lower()


@pytest.mark.asyncio
async def test_empty_report_counts_as_failure(tmp_path: Path) -> None:
    _write_type(tmp_path, "worker")
    catalog = SubAgentCatalog.load(tmp_path)

    outcomes = await run_sub_agents(
        [SubAgentTask("worker", "say nothing")],
        catalog=catalog,
        llm=_ReplyLLM(""),
        tool_registry=_registry(),
    )

    assert not outcomes[0].ok, "a sub-agent whose only product is a summary must produce one"


@pytest.mark.asyncio
async def test_task_count_ceiling_is_enforced(tmp_path: Path) -> None:
    _write_type(tmp_path, "worker")
    catalog = SubAgentCatalog.load(tmp_path)

    with pytest.raises(ValueError):
        await run_sub_agents(
            [SubAgentTask("worker", "x")] * (MAX_TASKS_PER_CALL + 1),
            catalog=catalog,
            llm=_ReplyLLM("hi"),
            tool_registry=_registry(),
        )


@pytest.mark.asyncio
async def test_no_tasks_is_a_no_op(tmp_path: Path) -> None:
    assert await run_sub_agents(
        [],
        catalog=SubAgentCatalog.load(SHIPPED_SUBAGENTS),
        llm=_ReplyLLM("hi"),
        tool_registry=_registry(),
    ) == []


# --------------------------------------------------------------------------
# Content-layer tool
# --------------------------------------------------------------------------


def _load_tool_module():
    path = ROOT / "agents" / "tools" / "sub_agent.py"
    spec = importlib.util.spec_from_file_location("sub_agent_provider", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_tool_rejects_malformed_tasks() -> None:
    tool = _load_tool_module()

    async def spawn(tasks, max_parallel):  # pragma: no cover - must not run
        raise AssertionError("validation should have stopped this")

    for bad in (None, [], "nonsense", [{"prompt": "x"}], [{"agent_type": "explorer"}]):
        result = await tool.execute(tasks=bad, _spawn=spawn, _agent_types=("explorer",))
        assert result.startswith("Error:"), bad


@pytest.mark.asyncio
async def test_tool_rejects_unknown_type_before_spawning() -> None:
    tool = _load_tool_module()

    async def spawn(tasks, max_parallel):  # pragma: no cover - must not run
        raise AssertionError("validation should have stopped this")

    result = await tool.execute(
        tasks=[{"agent_type": "ghost", "prompt": "go"}],
        _spawn=spawn,
        _agent_types=("explorer",),
    )
    assert result.startswith("Error:") and "explorer" in result


@pytest.mark.asyncio
async def test_tool_is_inert_without_the_injected_capability() -> None:
    tool = _load_tool_module()
    result = await tool.execute(tasks=[{"agent_type": "explorer", "prompt": "go"}])
    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_tool_renders_mixed_results() -> None:
    tool = _load_tool_module()
    seen: dict = {}

    async def spawn(tasks, max_parallel):
        seen["tasks"] = tasks
        seen["max_parallel"] = max_parallel
        return [
            {
                "label": "alpha", "agent_type": "explorer", "ok": True,
                "summary": "found it at foo.py:10", "error": "",
                "tool_calls": 3, "usage": {"total_tokens": 120},
            },
            {
                "label": "beta", "agent_type": "explorer", "ok": False,
                "summary": "", "error": "timed out after 600s",
                "tool_calls": 1, "usage": {},
            },
        ]

    result = await tool.execute(
        tasks=[
            {"agent_type": "explorer", "prompt": " go ", "label": "alpha"},
            {"agent_type": "explorer", "prompt": "go again", "label": "beta"},
        ],
        max_parallel=2,
        _spawn=spawn,
        _agent_types=("explorer",),
    )

    assert seen["tasks"][0]["prompt"] == "go"  # whitespace normalized
    assert seen["max_parallel"] == 2
    assert "1/2 succeeded" in result
    assert "[OK] alpha" in result and "[FAILED] beta" in result
    assert "found it at foo.py:10" in result
    assert "timed out after 600s" in result
    assert "3 tool calls" in result and "120 tokens" in result


@pytest.mark.asyncio
async def test_tool_truncates_a_runaway_summary() -> None:
    tool = _load_tool_module()

    async def spawn(tasks, max_parallel):
        return [{
            "label": "big", "agent_type": "explorer", "ok": True,
            "summary": "x" * 200_000, "error": "",
            "tool_calls": 0, "usage": {},
        }]

    result = await tool.execute(
        tasks=[{"agent_type": "explorer", "prompt": "go"}],
        _spawn=spawn,
        _agent_types=("explorer",),
    )
    assert "truncated" in result
    assert len(result.encode("utf-8")) <= tool.MAX_SUMMARY_BYTES + 1024


@pytest.mark.asyncio
async def test_a_full_fan_out_report_fits_under_the_runtime_truncation_ceiling() -> None:
    """Regression: 10 verbose agents overran the 50 KB tool-result ceiling.

    The runtime spills the overflow to a file, so the parent silently loses
    the *tail* — the findings of whichever sub-agents rendered last.
    """
    from engine.tool.truncation import MAX_BYTES

    tool = _load_tool_module()

    async def spawn(tasks, max_parallel):
        return [
            {
                "label": f"agent-{i}", "agent_type": "explorer", "ok": True,
                # CJK: three bytes per character, which is what broke a
                # character-based cap.
                "summary": "结论" * 20_000, "error": "",
                "tool_calls": 1, "usage": {},
            }
            for i in range(10)
        ]

    result = await tool.execute(
        tasks=[{"agent_type": "explorer", "prompt": f"go {i}"} for i in range(10)],
        _spawn=spawn,
        _agent_types=("explorer",),
    )

    assert len(result.encode("utf-8")) < MAX_BYTES, "report would be truncated by the runtime"
    # Every agent still present — no tail silently dropped.
    for i in range(10):
        assert f"agent-{i}" in result


@pytest.mark.asyncio
async def test_clipping_never_splits_a_multibyte_character() -> None:
    tool = _load_tool_module()
    # A budget that lands mid-character if bytes are sliced naively.
    clipped = tool._clip("汉" * 1000, 1001)
    assert "\ufffd" not in clipped
    clipped.encode("utf-8")  # must be valid text, not a broken sequence


def test_tool_meta_declares_an_honest_contract() -> None:
    tool = _load_tool_module()
    meta = tool.TOOL_META
    assert meta["name"] == SUB_AGENT_TOOL_NAME
    # A sub-agent may hold write tools, so the outer call is not a read.
    assert meta["permission_level"] == "execute"
    assert meta["side_effect"] == "write"
    assert meta["approval_policy"] == "policy"


# --------------------------------------------------------------------------
# Wiring: provider load → engine binding → real tool call
# --------------------------------------------------------------------------


def _services(
    tool_registry: ToolRegistry,
    llm: object,
    background_llm: object | None = None,
):
    """The real dataclass, not a hand-rolled double.

    A stub drifts silently every time the binding reads one more field; the
    production type cannot.
    """
    from engine.execution.orchestration.runtime import RuntimeServices
    from engine.skill.registry import SkillRegistry

    return RuntimeServices(
        llm=llm,
        tool_registry=tool_registry,
        skill_registry=SkillRegistry(),
        background_llm=background_llm,
    )


def _loaded_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.load_builtin_providers(ROOT / "agents" / "tools")
    return registry


def test_the_shipped_provider_is_on_the_builtin_allowlist() -> None:
    assert _loaded_registry().get(SUB_AGENT_TOOL_NAME) is not None, (
        "sub_agent.py must be in ToolRegistry._BUILTIN_PROVIDER_FILENAMES "
        "or the runtime never loads it"
    )


def test_binding_publishes_the_available_types_to_the_model() -> None:
    from engine.execution.orchestration.builtin_tools import bind_sub_agent_tool

    registry = _loaded_registry()
    bind_sub_agent_tool(_services(registry, _ReplyLLM("hi")), ROOT / "agents")

    definition = registry.get(SUB_AGENT_TOOL_NAME)
    assert not definition.hidden
    assert "explorer" in definition.description
    enum = definition.parameters["properties"]["tasks"]["items"]["properties"][
        "agent_type"
    ]["enum"]
    assert set(enum) == {"explorer", "reviewer", "implementer"}


def test_binding_hides_the_tool_when_no_types_are_installed(tmp_path: Path) -> None:
    from engine.execution.orchestration.builtin_tools import bind_sub_agent_tool

    registry = _loaded_registry()
    bind_sub_agent_tool(_services(registry, _ReplyLLM("hi")), tmp_path)

    definition = registry.get(SUB_AGENT_TOOL_NAME)
    assert definition.hidden, "an uninstallable capability must not reach the prompt"
    assert SUB_AGENT_TOOL_NAME not in {
        schema["function"]["name"] for schema in registry.get_schemas()
    }


def test_binding_hides_the_tool_when_the_catalog_is_malformed(tmp_path: Path) -> None:
    from engine.execution.orchestration.builtin_tools import bind_sub_agent_tool

    (tmp_path / "subagents").mkdir()
    (tmp_path / "subagents" / "bad.yaml").write_text("schema: nope/v1\n", encoding="utf-8")
    registry = _loaded_registry()
    bind_sub_agent_tool(_services(registry, _ReplyLLM("hi")), tmp_path)

    assert registry.get(SUB_AGENT_TOOL_NAME).hidden


@pytest.mark.asyncio
async def test_end_to_end_tool_call_spawns_and_summarizes(tmp_path: Path) -> None:
    """The full path a real turn takes: ToolCall in, rendered report out."""
    from engine.execution.orchestration.builtin_tools import bind_sub_agent_tool
    from engine.tool.interface import ToolCall

    agents_dir = tmp_path / "agents"
    (agents_dir / "subagents").mkdir(parents=True)
    _write_type(agents_dir / "subagents", "worker", tools=["read_file"])

    registry = _loaded_registry()
    bind_sub_agent_tool(_services(registry, _EchoPromptLLM()), agents_dir)

    result = await registry.execute(
        ToolCall(
            id="call-1",
            name=SUB_AGENT_TOOL_NAME,
            arguments={
                "tasks": [
                    {"agent_type": "worker", "prompt": "look at A", "label": "a"},
                    {"agent_type": "worker", "prompt": "look at B", "label": "b"},
                ],
                "max_parallel": 2,
            },
        )
    )

    assert not result.is_error, result.content
    assert "2/2 succeeded" in result.content
    assert "report for look at A" in result.content
    assert "report for look at B" in result.content


@pytest.mark.asyncio
async def test_a_model_supplied_spawn_argument_cannot_displace_the_real_one(
    tmp_path: Path,
) -> None:
    from engine.execution.orchestration.builtin_tools import bind_sub_agent_tool
    from engine.tool.interface import ToolCall

    agents_dir = tmp_path / "agents"
    (agents_dir / "subagents").mkdir(parents=True)
    _write_type(agents_dir / "subagents", "worker", tools=["read_file"])

    registry = _loaded_registry()
    bind_sub_agent_tool(_services(registry, _EchoPromptLLM()), agents_dir)

    result = await registry.execute(
        ToolCall(
            id="call-2",
            name=SUB_AGENT_TOOL_NAME,
            arguments={
                "tasks": [{"agent_type": "worker", "prompt": "look at A"}],
                "_spawn": "hijacked",
                "_agent_types": ["anything"],
            },
        )
    )

    assert not result.is_error, result.content
    assert "report for look at A" in result.content


@pytest.mark.asyncio
async def test_a_type_cannot_widen_the_parent_tool_allowlist(tmp_path: Path) -> None:
    """A declared tool the parent identity disabled stays disabled.

    ``ScopedToolRegistry`` intersects with the parent's enabled set, so a YAML
    author cannot hand a sub-agent a capability the profile turned off.
    """
    _write_type(tmp_path, "worker", tools=["echo_tool"])
    catalog = SubAgentCatalog.load(tmp_path)

    registry = _registry()
    registry.set_enabled([SUB_AGENT_TOOL_NAME])  # echo_tool disabled for this run
    llm = _RecursingLLM()
    # Reuse the recursion probe, pointed at echo_tool instead.
    original = llm.chat

    async def chat(messages, tools=None, prefix_cache_key=None):
        llm.calls += 1
        if llm.calls == 1:
            return ChatResponse(
                tool_calls=[ToolCallData(id="c1", name="echo_tool", arguments={"text": "hi"})]
            )
        llm.tool_reply = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "tool"), ""
        )
        return ChatResponse(text="finished")

    llm.chat = chat
    del original

    outcomes = await run_sub_agents(
        [SubAgentTask("worker", "use the disabled tool")],
        catalog=catalog,
        llm=llm,
        tool_registry=registry,
    )

    assert outcomes[0].ok
    # Non-vacuous: the call must actually have been made and refused, not
    # simply never attempted.
    assert llm.tool_reply, "the sub-agent never reached the tool result"
    assert "echo:hi" not in llm.tool_reply, "a disabled tool must not execute"
    assert "disabled" in llm.tool_reply.lower()


# --------------------------------------------------------------------------
# Model role
# --------------------------------------------------------------------------


class _NamedLLM:
    """Reports which port answered, so role routing is observable."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def chat(self, messages, tools=None, prefix_cache_key=None):
        self.calls += 1
        return ChatResponse(text=f"answered by {self.name}")


def test_invalid_model_role_is_rejected(tmp_path: Path) -> None:
    _write_type(tmp_path, "wrong", model="gpt-9-turbo")
    with pytest.raises(SubAgentCatalogError):
        SubAgentCatalog.load(tmp_path)


def test_model_role_defaults_to_interactive(tmp_path: Path) -> None:
    _write_type(tmp_path, "plain")
    assert SubAgentCatalog.load(tmp_path).get("plain").model == "interactive"


@pytest.mark.asyncio
async def test_declared_model_role_selects_that_port(tmp_path: Path) -> None:
    _write_type(tmp_path, "cheap", model="background")
    catalog = SubAgentCatalog.load(tmp_path)
    interactive, background = _NamedLLM("interactive"), _NamedLLM("background")

    outcomes = await run_sub_agents(
        [SubAgentTask("cheap", "go")],
        catalog=catalog,
        llm=interactive,
        background_llm=background,
        tool_registry=_registry(),
    )

    assert outcomes[0].summary == "answered by background"
    assert outcomes[0].model_role == "background"
    assert interactive.calls == 0, "the interactive port must not be touched"


@pytest.mark.asyncio
async def test_missing_background_port_falls_back_instead_of_failing(
    tmp_path: Path,
) -> None:
    """A deployment without a background client still runs background types."""
    _write_type(tmp_path, "cheap", model="background")
    catalog = SubAgentCatalog.load(tmp_path)
    interactive = _NamedLLM("interactive")

    outcomes = await run_sub_agents(
        [SubAgentTask("cheap", "go")],
        catalog=catalog,
        llm=interactive,
        background_llm=None,
        tool_registry=_registry(),
    )

    assert outcomes[0].ok
    assert outcomes[0].summary == "answered by interactive"


# --------------------------------------------------------------------------
# Token budget
# --------------------------------------------------------------------------


class _BurningLLM:
    """Never terminates on its own; bills a fixed cost per turn."""

    def __init__(self, per_turn: int = 1000, report_total: bool = True) -> None:
        self.per_turn = per_turn
        self.report_total = report_total
        self.calls = 0

    async def chat(self, messages, tools=None, prefix_cache_key=None):
        self.calls += 1
        usage = {"prompt_tokens": self.per_turn, "completion_tokens": 0}
        if self.report_total:
            usage["total_tokens"] = self.per_turn
        return ChatResponse(
            tool_calls=[ToolCallData(id=f"c{self.calls}", name="echo_tool",
                                     arguments={"text": "x"})],
            usage=usage,
        )


def test_invalid_token_budget_is_rejected(tmp_path: Path) -> None:
    _write_type(tmp_path, "wrong", token_budget=0)
    with pytest.raises(SubAgentCatalogError):
        SubAgentCatalog.load(tmp_path)


def test_declared_token_budget_is_capped(tmp_path: Path) -> None:
    from engine.execution.subagent.catalog import MAX_DECLARED_TOKEN_BUDGET

    _write_type(tmp_path, "greedy", token_budget=99_000_000)
    spec = SubAgentCatalog.load(tmp_path).get("greedy")
    assert spec.token_budget == MAX_DECLARED_TOKEN_BUDGET


@pytest.mark.asyncio
async def test_per_agent_token_budget_stops_a_runaway(tmp_path: Path) -> None:
    _write_type(tmp_path, "runaway", max_iters=40, token_budget=3000)
    catalog = SubAgentCatalog.load(tmp_path)
    llm = _BurningLLM(per_turn=1000)

    outcomes = await run_sub_agents(
        [SubAgentTask("runaway", "spin forever")],
        catalog=catalog,
        llm=llm,
        tool_registry=_registry(),
    )

    assert not outcomes[0].ok
    assert "token budget exhausted" in outcomes[0].error
    # Stopped on budget, well before the 40-iteration ceiling would have.
    assert llm.calls <= 5, f"budget did not stop the loop promptly ({llm.calls} turns)"
    assert outcomes[0].usage["total_tokens"] >= 3000


@pytest.mark.asyncio
async def test_usage_without_total_tokens_still_charges_the_budget(
    tmp_path: Path,
) -> None:
    """A provider that omits ``total_tokens`` must not read as free."""
    _write_type(tmp_path, "runaway", max_iters=40, token_budget=3000)
    catalog = SubAgentCatalog.load(tmp_path)
    llm = _BurningLLM(per_turn=1000, report_total=False)

    outcomes = await run_sub_agents(
        [SubAgentTask("runaway", "spin forever")],
        catalog=catalog,
        llm=llm,
        tool_registry=_registry(),
    )

    assert not outcomes[0].ok
    assert "budget exhausted" in outcomes[0].error
    assert llm.calls <= 5


@pytest.mark.asyncio
async def test_batch_budget_bounds_the_whole_fan_out(tmp_path: Path) -> None:
    _write_type(tmp_path, "runaway", max_iters=40, token_budget=1_000_000)
    catalog = SubAgentCatalog.load(tmp_path)
    llm = _BurningLLM(per_turn=1000)

    outcomes = await run_sub_agents(
        [SubAgentTask("runaway", f"task {i}") for i in range(4)],
        catalog=catalog,
        llm=llm,
        tool_registry=_registry(),
        max_parallel=2,
        batch_token_budget=5000,
    )

    assert not any(o.ok for o in outcomes)
    assert any("batch token budget" in o.error for o in outcomes)
    # The batch ceiling, not the per-agent one, is what stopped the spend.
    assert llm.calls <= 12, f"batch budget leaked ({llm.calls} provider calls)"


# --------------------------------------------------------------------------
# Review regressions: what fan-out broke that serial execution never could
# --------------------------------------------------------------------------


class _ApprovingLLM:
    """Calls a tool once, then reports whatever the tool result said."""

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        self.calls = 0
        self.tool_reply = ""

    async def chat(self, messages, tools=None, prefix_cache_key=None):
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(
                tool_calls=[
                    ToolCallData(id="c1", name=self.tool_name, arguments={"text": "x"})
                ]
            )
        self.tool_reply = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "tool"), ""
        )
        return ChatResponse(text="reported")


@pytest.mark.asyncio
async def test_a_sub_agent_never_blocks_on_the_users_approval_broker(
    tmp_path: Path,
) -> None:
    """Regression: the sub-agent inherited the parent's broker and hung.

    Its approval request is emitted as an event the runner discards, so the
    user never sees the prompt — the loop then sat in ``broker.wait`` until
    the task timeout, burning the whole budget for nothing.
    """
    from engine.safety.approval import ApprovalBroker, use_approval_context
    from engine.safety.tool_guard import ToolGuard

    registry = ToolRegistry()
    registry.register(
        "needs_approval",
        "A tool that always requires approval",
        {"properties": {"text": {"type": "string"}}, "required": ["text"]},
        lambda text: f"did:{text}",
        permission_level="write",
        approval_policy="always",
        side_effect="write",
    )
    guard = ToolGuard(tmp_path / "rules.json", allowed_dirs=[tmp_path])
    guard.bind_definitions(registry.definitions())
    registry.bind_tool_guard(guard)

    _write_type(tmp_path, "worker", tools=["needs_approval"])
    catalog = SubAgentCatalog.load(tmp_path)
    llm = _ApprovingLLM("needs_approval")

    # Nobody will ever answer this broker — exactly the production situation.
    with use_approval_context(ApprovalBroker(), "run-1"):
        outcomes = await asyncio.wait_for(
            run_sub_agents(
                [SubAgentTask("worker", "do the thing")],
                catalog=catalog,
                llm=llm,
                tool_registry=registry,
                tool_guard=guard,
            ),
            timeout=5,  # fails loudly if the hang comes back
        )

    assert outcomes[0].ok
    assert llm.tool_reply, "the sub-agent never saw a tool result"
    assert "did:x" not in llm.tool_reply, "an unapproved side effect ran"


class _ToolThenAnswerLLM:
    """Calls a tool, then answers. Stateless, so parallel agents can share it."""

    async def chat(self, messages, tools=None, prefix_cache_key=None):
        already_used_a_tool = any(m.get("role") == "tool" for m in messages)
        if already_used_a_tool:
            return ChatResponse(text="done")
        return ChatResponse(
            tool_calls=[ToolCallData(id="c1", name="echo_tool", arguments={"text": "x"})]
        )


@pytest.mark.asyncio
async def test_each_sub_agent_gets_its_own_fact_gate(tmp_path: Path) -> None:
    """Regression: parallel agents shared the parent's mutable gate.

    ``begin_round()`` moves pending challenges into ``_checked``; on a shared
    instance one agent's round boundary silently satisfies a sibling's
    outstanding challenge, defeating the gate. The sub-agents below must make
    real tool calls — ``begin_round()`` only runs on a round that has them, so
    a tool-free probe would pass either way.
    """
    from engine.safety.fact_gate import FactGate, FactGateContext, use_fact_gate

    _write_type(tmp_path, "worker", tools=["echo_tool"])
    catalog = SubAgentCatalog.load(tmp_path)
    parent = FactGate(FactGateContext(session_id="s1", turn_id="t1"), enabled=True)
    parent._pending.add("sentinel")

    with use_fact_gate(parent):
        outcomes = await run_sub_agents(
            [SubAgentTask("worker", f"task {i}") for i in range(3)],
            catalog=catalog,
            llm=_ToolThenAnswerLLM(),
            tool_registry=_registry(),
            max_parallel=3,
        )

    assert all(o.ok for o in outcomes), [o.error for o in outcomes]
    assert sum(o.tool_calls for o in outcomes) == 3, "no round boundary was crossed"
    # No sub-agent's round boundary may advance the parent's gate.
    assert parent._pending == {"sentinel"}
    assert parent._checked == set()


class _BlockingHook:
    """A PreToolHook that refuses everything, to prove hooks reach sub-agents."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    @property
    def id(self) -> str:
        return "test-blocker"

    @property
    def priority(self) -> int:
        return 1

    @property
    def enabled(self) -> bool:
        return True

    async def check(self, tool_name: str, tool_input: dict) -> tuple[bool, str | None]:
        self.seen.append(tool_name)
        return False, "blocked by test hook"


@pytest.mark.asyncio
async def test_pre_tool_hooks_run_for_delegated_work(tmp_path: Path) -> None:
    """Regression: hooks were not passed through, so config-protection et al.

    simply did not apply to sub-agents — delegated work could edit files the
    parent is blocked from touching.
    """
    from engine.execution.hooks import HookRegistry

    _write_type(tmp_path, "worker", tools=["echo_tool"])
    catalog = SubAgentCatalog.load(tmp_path)
    hooks = HookRegistry()
    blocker = _BlockingHook()
    hooks.register_pre_hook(blocker)
    llm = _ApprovingLLM("echo_tool")

    outcomes = await run_sub_agents(
        [SubAgentTask("worker", "try the tool")],
        catalog=catalog,
        llm=llm,
        tool_registry=_registry(),
        hook_registry=hooks,
    )

    assert outcomes[0].ok
    assert blocker.seen == ["echo_tool"], "the pre-tool hook never ran"
    assert "echo:x" not in llm.tool_reply, "a hook-blocked tool still executed"
    assert "blocked by test hook" in llm.tool_reply


def test_the_sub_agent_prompt_names_its_tools(tmp_path: Path) -> None:
    """Regression: under lazy-schema mode the model had to guess tool names.

    A sub-agent's prompt is not built by PromptAssembler, so it carries no
    "Available Tools" layer; the runtime hands it only a schema *loader*.
    """
    from engine.execution.subagent.runner import _conversation

    _write_type(tmp_path, "worker", tools=["read_file", "grep"])
    spec = SubAgentCatalog.load(tmp_path).get("worker")
    system = _conversation(spec, SubAgentTask("worker", "go"))[0]["content"]

    assert "read_file" in system and "grep" in system


@pytest.mark.asyncio
async def test_a_sub_agent_works_under_the_runtimes_lazy_schema_mode(
    tmp_path: Path,
) -> None:
    """Production builds the registry with ``lazy_tool_schemas=True``.

    In that mode the model is handed only a schema *loader*, and the scoped
    view — not the parent registry — must answer the load. Every other test
    here runs eager, so this is the one that exercises the real config.
    """
    from engine.execution.react.react_loop import _TOOL_SCHEMA_LOADER

    registry = ToolRegistry(lazy_tool_schemas=True)
    registry.register(
        "echo_tool", "Echo a string",
        {"properties": {"text": {"type": "string"}}, "required": ["text"]},
        lambda text: f"echo:{text}",
        permission_level="read", approval_policy="never", side_effect="none",
    )
    registry.register(
        "forbidden_tool", "Must stay out of reach",
        {"properties": {}}, lambda: "leaked",
        permission_level="read", approval_policy="never", side_effect="none",
    )
    _write_type(tmp_path, "worker", tools=["echo_tool"])
    catalog = SubAgentCatalog.load(tmp_path)

    seen: dict[str, str] = {}

    class _LazyLLM:
        async def chat(self, messages, tools=None, prefix_cache_key=None):
            offered = {schema["function"]["name"] for schema in (tools or [])}
            seen.setdefault("offered", ",".join(sorted(offered)))
            replies = [m for m in messages if m.get("role") == "tool"]
            if not replies:
                # Try the out-of-scope tool first — the scoped view must refuse.
                return ChatResponse(tool_calls=[
                    ToolCallData(id="s1", name=_TOOL_SCHEMA_LOADER,
                                 arguments={"name": "forbidden_tool"}),
                    ToolCallData(id="s2", name=_TOOL_SCHEMA_LOADER,
                                 arguments={"name": "echo_tool"}),
                ])
            if len(replies) == 2:
                seen["forbidden"] = replies[0]["content"]
                seen["allowed"] = replies[1]["content"]
                return ChatResponse(tool_calls=[
                    ToolCallData(id="c1", name="echo_tool", arguments={"text": "hi"})
                ])
            seen["result"] = replies[-1]["content"]
            return ChatResponse(text="reported")

    outcomes = await run_sub_agents(
        [SubAgentTask("worker", "use your tool")],
        catalog=catalog,
        llm=_LazyLLM(),
        tool_registry=registry,
    )

    assert outcomes[0].ok, outcomes[0].error
    # Only the loader is offered up front — that is what lazy mode means.
    assert seen["offered"] == _TOOL_SCHEMA_LOADER
    # The scope holds even through the loader.
    assert "unavailable" in seen["forbidden"].lower()
    assert "echo_tool" in seen["allowed"]
    assert seen["result"] == "echo:hi"


# --------------------------------------------------------------------------
# Second review pass
# --------------------------------------------------------------------------


def test_binding_twice_does_not_duplicate_the_type_listing() -> None:
    """Regression: the binding appended unconditionally.

    Each extra bind added the whole catalogue again to the tool's public
    description. Today every turn builds a fresh registry so it never fires —
    but a binding that corrupts its own contract when called twice is a trap
    for whoever caches services next.
    """
    from engine.execution.orchestration.builtin_tools import bind_sub_agent_tool

    registry = _loaded_registry()
    services = _services(registry, _ReplyLLM("hi"))

    bind_sub_agent_tool(services, ROOT / "agents")
    once = registry.get(SUB_AGENT_TOOL_NAME).description
    bind_sub_agent_tool(services, ROOT / "agents")
    twice = registry.get(SUB_AGENT_TOOL_NAME).description

    assert once == twice
    assert twice.count("Available sub-agent types:") == 1


def test_a_type_whose_only_tool_is_the_spawn_tool_is_rejected(tmp_path: Path) -> None:
    """Regression: 'at least one tool' was checked before the recursion strip.

    ``tools: [sub_agent]`` passed validation and then produced a type with an
    empty tool set — an agent that can do nothing but talk.
    """
    _write_type(tmp_path, "hollow", tools=[SUB_AGENT_TOOL_NAME])
    with pytest.raises(SubAgentCatalogError, match="other than"):
        SubAgentCatalog.load(tmp_path)


def test_no_shipped_type_can_write_through_a_tool_it_does_not_advertise() -> None:
    """Types described as read-only must hold no write-capable tool.

    Scoping is per tool *name*, not per action: ``git_ops`` declares
    ``read_actions`` but carries commit/push/branch in the same tool, so a
    "read-only" reviewer holding it could push. The parent supplies the diff
    in the brief instead.
    """
    registry = _loaded_registry()
    catalog = SubAgentCatalog.load(SHIPPED_SUBAGENTS)
    read_only = {"explorer", "reviewer"}

    for spec in catalog.specs:
        if spec.id not in read_only:
            continue
        for name in spec.tools:
            definition = registry.get(name)
            assert definition is not None, f"{spec.id} declares unknown tool {name}"
            assert not definition.is_write_tool, (
                f"{spec.id} is described as read-only but holds write tool {name}"
            )
            assert definition.permission_level in ("read", ""), (
                f"{spec.id} holds {name} at permission_level={definition.permission_level}"
            )


def test_no_shipped_type_holds_session_shared_or_memory_tools() -> None:
    """A sub-agent must not reach the user's durable state.

    ``todo`` is session-scoped and index-addressed — parallel siblings would
    overwrite each other's entries, and it is the user's visible task list,
    not scratch space. ``memory_ops`` would let ephemeral work write durable
    memory nobody reviewed.
    """
    forbidden = {"todo", "memory_ops", "skill_manage", SUB_AGENT_TOOL_NAME}
    for spec in SubAgentCatalog.load(SHIPPED_SUBAGENTS).specs:
        assert not forbidden.intersection(spec.tools), (
            f"{spec.id} holds session-shared or durable-state tools: "
            f"{sorted(forbidden.intersection(spec.tools))}"
        )


def test_every_shipped_type_declares_tools_that_actually_exist() -> None:
    registry = _loaded_registry()
    for spec in SubAgentCatalog.load(SHIPPED_SUBAGENTS).specs:
        for name in spec.tools:
            assert registry.get(name) is not None, (
                f"{spec.id} declares {name}, which no shipped provider registers"
            )
