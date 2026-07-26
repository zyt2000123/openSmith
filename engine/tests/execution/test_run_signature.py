from engine.execution.events import EventType, ExecutionEvent
from engine.execution.run_signature import signature_diff, signature_of


def test_signature_diff_names_the_first_diverging_tool() -> None:
    expected = signature_of([
        ExecutionEvent(EventType.TOOL_CALL_START, {"name": "read_file"}),
        ExecutionEvent(EventType.DONE, {}),
    ])
    actual = signature_of([
        ExecutionEvent(EventType.TOOL_CALL_START, {"name": "shell"}),
        ExecutionEvent(EventType.DONE, {}),
    ])

    diff = signature_diff(expected, actual)

    assert "tool[0]" in diff
    assert "read_file" in diff and "shell" in diff


def test_signature_diff_reports_a_missing_event() -> None:
    expected = signature_of([
        ExecutionEvent(EventType.TOOL_CALL_START, {"name": "ok"}),
        ExecutionEvent(EventType.DONE, {}),
    ])
    actual = signature_of([
        ExecutionEvent(EventType.TOOL_CALL_START, {"name": "ok"}),
    ])

    diff = signature_diff(expected, actual)

    assert "event count" in diff
    assert signature_diff(expected, expected) == ""
