from __future__ import annotations

import asyncio
from pathlib import Path

from engine.observability.events import EventType
from engine.execution.react.react_loop import react_event_loop
from engine.execution.react.smith_ui import validate_smith_ui_call
from engine.llm.client import ChatResponse, ToolCallData
from engine.tool.registry import ToolRegistry


def _heading_spec() -> dict:
    return {
        "root": "summary",
        "elements": {
            "summary": {
                "type": "Heading",
                "props": {"text": "Deployment", "level": "h1"},
                "children": [],
            }
        },
    }


def test_smith_ui_validation_accepts_an_allowed_tree_and_local_image(tmp_path: Path) -> None:
    image = tmp_path / "chart.png"
    image.write_bytes(b"not-a-real-image")

    result = validate_smith_ui_call(
        {"spec": _heading_spec(), "images": [{"path": "chart.png", "alt": "Build chart", "width": 24}]},
        working_dir=tmp_path,
    )

    assert result.ok is True
    assert result.payload == {
        "version": 1,
        "spec": _heading_spec(),
        "images": [{"path": str(image.resolve()), "alt": "Build chart", "width": 24}],
    }


def test_smith_ui_validation_rejects_unapproved_components_and_remote_images(tmp_path: Path) -> None:
    bad_component = validate_smith_ui_call(
        {
            "spec": {
                "root": "input",
                "elements": {"input": {"type": "TextInput", "props": {}, "children": []}},
            }
        },
        working_dir=tmp_path,
    )
    remote_image = validate_smith_ui_call(
        {"spec": _heading_spec(), "images": [{"path": "https://example.test/chart.png", "alt": "remote"}]},
        working_dir=tmp_path,
    )

    assert bad_component.ok is False
    assert "not permitted" in bad_component.reason
    assert remote_image.ok is False
    assert "local" in remote_image.reason


class _FakeLlm:
    stream = False

    def __init__(self) -> None:
        self.responses = [
            ChatResponse(
                tool_calls=[ToolCallData(id="ui-1", name="render_ui", arguments={"spec": _heading_spec()})]
            ),
            ChatResponse(text="The deployment summary is shown above."),
        ]

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> ChatResponse:
        return self.responses.pop(0)


def test_react_loop_emits_validated_smith_ui_without_executing_a_presentation_tool(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register("render_ui", "Render a validated terminal UI", {"type": "object"}, lambda: "must not run")
    registry.bind_working_directory(tmp_path)

    async def run():
        return [
            event
            async for event in react_event_loop(
                _FakeLlm(),
                [{"role": "user", "content": "show deployment"}],
                registry,
            )
        ]

    events = asyncio.run(run())

    ui = [event for event in events if event.type is EventType.SMITH_UI]
    assert [event.data for event in ui] == [{"version": 1, "spec": _heading_spec(), "images": []}]
    assert not [event for event in events if event.type is EventType.TOOL_CALL_START]
    assert any(event.type is EventType.TEXT_DELTA for event in events)


class _InvalidUiFakeLlm:
    stream = False

    def __init__(self) -> None:
        self.responses = [
            ChatResponse(
                tool_calls=[
                    ToolCallData(
                        id="ui-invalid",
                        name="render_ui",
                        arguments={
                            "spec": {
                                "root": "input",
                                "elements": {"input": {"type": "TextInput", "props": {}, "children": []}},
                            }
                        },
                    )
                ]
            ),
            ChatResponse(text="I used the safe fallback."),
        ]

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> ChatResponse:
        return self.responses.pop(0)


def test_react_loop_falls_back_to_a_code_block_event_for_an_invalid_ui_spec(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register("render_ui", "Render a validated terminal UI", {"type": "object"}, lambda: "must not run")
    registry.bind_working_directory(tmp_path)

    async def run():
        return [
            event
            async for event in react_event_loop(
                _InvalidUiFakeLlm(),
                [{"role": "user", "content": "show deployment"}],
                registry,
            )
        ]

    events = asyncio.run(run())

    fallback = [event for event in events if event.type is EventType.SMITH_UI_FALLBACK]
    assert len(fallback) == 1
    assert "not permitted" in fallback[0].data["reason"]
    assert '"TextInput"' in fallback[0].data["code"]
    assert not [event for event in events if event.type is EventType.TOOL_CALL_START]
