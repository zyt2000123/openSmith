"""真调 LLM 的端到端冒烟测试 —— 默认跳过，手动触发。

启用：
    AGENT_SMITH_E2E=1 uv run --with pytest --with pytest-asyncio pytest tests/test_e2e_smoke.py -v

顺便录 golden case（之后可用 engine.replay 无成本重放）：
    AGENT_SMITH_E2E=1 AGENT_SMITH_RECORD_LLM=~/.agent-smith/golden/write.jsonl \
        uv run --with pytest --with pytest-asyncio pytest tests/test_e2e_smoke.py -k write

这一层捕的是 636 条 mock 测试碰不到的故障：接线断了但每个零件都对。
它**不是** eval —— 样本太少、模型有随机性，只能答"通不通"，答不了"变好还是变坏"。

断言只看两样东西：**文件系统的真实变化** 和 **工具调用序列**。
刻意不断言回复文本 —— 模型换个说法就会让脆断言变红，而世界状态不会骗人。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

from app.services.engine_runtime import build_engine_runtime
from common.config import AGENT_DIR
from engine.execution.orchestration.agent_loop import run_stream_with_runtime
from engine.execution.orchestration.run_state import RunStateStore, RunStatus
from engine.execution.orchestration.runtime import EngineRequest
from engine.observability.events import EventType
from engine.replay import signature_of
from engine.safety.approval import APPROVAL_BROKER

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENT_SMITH_E2E"),
    reason="真调 LLM 会花钱；手动启用 AGENT_SMITH_E2E=1",
)


@dataclass(frozen=True)
class Case:
    """一个端到端任务：自然语言进，世界状态出。"""

    name: str
    message: str
    setup: Callable[[Path], None] | None
    check: Callable[[Path, tuple[str, ...]], None]
    max_tool_calls: int


async def _auto_approve(stop: asyncio.Event) -> None:
    """模拟用户在 UI 上点"确认"。

    写类工具（write_file / edit_file / shell）会触发审批并阻塞在
    ``ApprovalBroker.wait()``，默认 300 秒超时。真实运行里是用户点确认；
    测试里没有 UI，缺了这个应答方，每条写操作 case 都会挂满整个超时
    —— 实测 write 一条就撑爆 150 秒（no_tool 2.9s / read 5.8s 对照）。
    """
    store = RunStateStore(AGENT_DIR)
    while not stop.is_set():
        for path in store.root.glob("*.json"):
            try:
                state = store.get(path.stem)
            except Exception:  # 半写入的 run state 不该拖垮 approver
                continue
            if (
                state is not None
                and state.status is RunStatus.WAITING_APPROVAL
                and state.approval_id
            ):
                APPROVAL_BROKER.resolve(state.run_id, state.approval_id, True)
        await asyncio.sleep(0.1)


async def _run(message: str, working_dir: Path) -> tuple[str, ...]:
    """跑一次完整 run，返回工具调用名序列。"""
    runtime, services = build_engine_runtime(
        "smith", "Smith", session_id=f"e2e-{uuid.uuid4().hex[:8]}"
    )
    stop = asyncio.Event()
    approver = asyncio.create_task(_auto_approve(stop))
    stream = run_stream_with_runtime(
        EngineRequest(message=message, working_dir=str(working_dir)),
        runtime,
        services,
    )
    events = []
    try:
        async for event in stream.stream_events():
            events.append(event)
    finally:
        stop.set()
        approver.cancel()
        await stream.aclose()
    # 必须先确认 run 真的收尾了。否则 run 崩掉时工具序列同样是空的，
    # 而 _check_no_tool 断言的正是"空"—— 一条挂掉的 run 会被当成通过。
    # （实测教训：no_tool 曾在 1.72s "通过"，因为 run 根本没跑起来。）
    kinds = [event.type for event in events]
    if EventType.DONE not in kinds:
        raise AssertionError(
            f"run 未正常收尾，末尾事件：{[k.value for k in kinds[-4:]]}"
        )
    return signature_of(events).tools


def _check_write(workspace: Path, tools: tuple[str, ...]) -> None:
    target = workspace / "a.txt"
    assert target.is_file(), f"a.txt 未被创建（工具调用：{tools}）"
    assert "fooooo" in target.read_text(encoding="utf-8")


def _check_edit(workspace: Path, tools: tuple[str, ...]) -> None:
    content = (workspace / "c.txt").read_text(encoding="utf-8")
    assert "new" in content, f"替换未生效（工具调用：{tools}）"
    assert "old" not in content, f"旧内容仍在（工具调用：{tools}）"


def _check_no_tool(workspace: Path, tools: tuple[str, ...]) -> None:
    # 最有价值的一条断言：简单问答不该动工具。
    # 它捕的是"模型开始滥用工具"这类退化 —— 功能全对，但每次都绕一圈。
    assert tools == (), f"1+1 不需要任何工具，实际调用了 {tools}"


def _check_read_happened(workspace: Path, tools: tuple[str, ...]) -> None:
    assert tools, "读取文件必须至少调用一个工具"


def _check_append(workspace: Path, tools: tuple[str, ...]) -> None:
    lines = [
        line
        for line in (workspace / "d.txt").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) >= 2, f"追加后应至少两行，实际 {lines!r}（工具调用：{tools}）"
    assert any("second" in line for line in lines)


CASES = [
    Case(
        name="write",
        message="在当前目录新建 a.txt，内容写 fooooo",
        setup=None,
        check=_check_write,
        # 2026-07-25 校准：独立跑 5 次，稳定 2 次 —— ('write_file', 'write_file')。
        # 但在 e2e 连跑中观察到过峰值 4（含一次 shell），故上限取 4：覆盖已见峰值，
        # 超过 4 才值得当退化看。
        # 可优化信号（非 bug）：写一个文件稳定用了两次 write_file。
        max_tool_calls=4,
    ),
    Case(
        name="edit",
        message="把 c.txt 里的 old 改成 new",
        setup=lambda ws: (ws / "c.txt").write_text("old\n", encoding="utf-8"),
        check=_check_edit,
        # 2026-07-25 实测基线 4 次：glob_files → read_file → edit_file → edit_file。
        # 上限留到 5 而不是卡在实测值 —— 模型有随机性，卡死会产生假红灯，
        # 而假红灯会训练人忽略红灯。超过 5 才说明轨迹真的退化了。
        # 可优化信号（非 bug）：消息里已给出确切文件名，模型仍先 glob 了一次。
        max_tool_calls=5,
    ),
    Case(
        name="no_tool",
        message="1+1 等于几？直接回答",
        setup=None,
        check=_check_no_tool,
        max_tool_calls=0,
    ),
    Case(
        name="read",
        message="b.txt 里写了什么？",
        setup=lambda ws: (ws / "b.txt").write_text("hello from b\n", encoding="utf-8"),
        check=_check_read_happened,
        max_tool_calls=2,
    ),
    Case(
        name="append",
        message="在 d.txt 末尾追加一行 second",
        setup=lambda ws: (ws / "d.txt").write_text("first\n", encoding="utf-8"),
        check=_check_append,
        # 2026-07-25 实测基线 5 次：
        # glob_files → read_file → write_file → write_file → read_file（末次为回读自检）。
        # 上限取实测 +2 —— 上限必须来自实测，不能凭想象填（初版填 3，两条写操作全红）。
        max_tool_calls=7,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
async def test_e2e_smoke(case: Case, tmp_path: Path) -> None:
    workspace = tmp_path / case.name
    workspace.mkdir()
    if case.setup is not None:
        case.setup(workspace)

    tools = await _run(case.message, workspace)

    case.check(workspace, tools)
    assert len(tools) <= case.max_tool_calls, (
        f"轨迹退化：{case.name} 用了 {len(tools)} 次工具调用 "
        f"（上限 {case.max_tool_calls}）：{tools}"
    )
