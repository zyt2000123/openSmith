"""Unit tests for Module 3: ephemeral / read_only_tools engine mode (used by /side)."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.execution.orchestration.lifecycle import run_stream_with_runtime
from engine.execution.orchestration.preparation import prepare_runtime
from engine.execution.orchestration.runtime import (
    EngineRequest,
    RuntimeContext,
    RuntimeServices,
)
from engine.llm.contracts import ChatResponse
from engine.safety.tool_guard import ToolGuard
from engine.skill.registry import SkillRegistry
from engine.tool.registry import ToolRegistry


class _StaticLLM:
    model = "test-model"
    provider = "openai"

    async def chat(self, messages, tools=None, **kwargs) -> ChatResponse:
        return ChatResponse(text="ephemeral answer", tool_calls=[])

    async def close(self) -> None:
        return None


def _setup_profile(tmp_path: Path) -> tuple[Path, Path, Path]:
    data_dir = tmp_path / "data"
    profile_dir = data_dir / "agent"
    profile_dir.mkdir(parents=True)
    (profile_dir / "role.md").write_text("# Role\nSmith assistant.\n", encoding="utf-8")
    (profile_dir / "style.md").write_text("# Style\nConcise.\n", encoding="utf-8")
    (profile_dir / "workflow.md").write_text("# Workflow\nBe helpful.\n", encoding="utf-8")
    (profile_dir / "context.md").write_text("# User\nPrefers terse output.\n", encoding="utf-8")
    mem_dir = profile_dir / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "durable.md").write_text("# Durable\nProject uses FastAPI.\n", encoding="utf-8")
    work_dir = tmp_path / "workspace"
    work_dir.mkdir()
    agents_dir = Path(__file__).resolve().parents[3] / "agents"
    return profile_dir, work_dir, agents_dir


@pytest.mark.asyncio
async def test_read_only_tools_narrows_registry_to_pure_reads(tmp_path: Path):
    """read_only_tools=True strips write/execute/serial tools while keeping pure read tools and durable memory."""
    profile_dir, work_dir, agents_dir = _setup_profile(tmp_path)

    registry = ToolRegistry()
    skills = SkillRegistry()
    guard = ToolGuard(tmp_path / "rules.json")
    services = RuntimeServices(
        llm=_StaticLLM(),
        tool_registry=registry,
        skill_registry=skills,
        tool_guard=guard,
    )
    runtime = RuntimeContext(
        agent_id="smith-1",
        agent_name="Smith",
        profile_dir=profile_dir,
        agents_dir=agents_dir,
        session_id="side-session-1",
    )
    request = EngineRequest(
        message="explain how routing works",
        working_dir=str(work_dir),
        ephemeral=True,
        read_only_tools=True,
    )

    setup = await prepare_runtime(request, runtime, services)
    enabled_names = {defn.name for defn in services.tool_registry.list_tools()}

    # Pure read tools stay available
    assert "read_file" in enabled_names
    assert "grep" in enabled_names
    assert "glob_files" in enabled_names

    # Write / execute / stateful tools are completely removed
    for forbidden in (
        "write_file",
        "edit_file",
        "shell",
        "git_ops",
        "todo",
        "memory_ops",
        "skill_manage",
        "sub_agent",
    ):
        assert forbidden not in enabled_names, f"{forbidden} must be disabled in read_only_tools mode"

    # Durable memory is still visible read-only
    assert "Project uses FastAPI." in setup.system_prompt


@pytest.mark.asyncio
async def test_ephemeral_run_skips_runtime_learning_persistence(tmp_path: Path):
    """ephemeral=True runs cleanly without writing anything to memory/recent.jsonl."""
    profile_dir, work_dir, agents_dir = _setup_profile(tmp_path)
    recent_log = profile_dir / "memory" / "recent.jsonl"
    assert not recent_log.exists()

    registry = ToolRegistry()
    skills = SkillRegistry()
    guard = ToolGuard(tmp_path / "rules.json")
    services = RuntimeServices(
        llm=_StaticLLM(),
        tool_registry=registry,
        skill_registry=skills,
        tool_guard=guard,
    )
    runtime = RuntimeContext(
        agent_id="smith-1",
        agent_name="Smith",
        profile_dir=profile_dir,
        agents_dir=agents_dir,
        session_id="side-session-2",
    )
    request = EngineRequest(
        message="Remember: always use tabs instead of spaces",
        working_dir=str(work_dir),
        ephemeral=True,
        read_only_tools=True,
    )

    stream = run_stream_with_runtime(request, runtime, services)
    events = [ev async for ev in stream.stream_events()]
    assert stream.status == "completed"
    assert any(ev.data.get("text") == "ephemeral answer" for ev in events)
    assert not recent_log.exists()
