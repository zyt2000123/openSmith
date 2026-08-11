"""Declarative presentation tool registered for the engine-owned UI event path."""
# 只提交声明式 UI 规格；实际渲染与事件通道由 engine 统一掌控。

TOOL_META = {
    "name": "render_ui",
    "description": (
        "Render a concise, declarative terminal UI when a card, key/value view, table, "
        "status, progress, chart, or local image adds clarity. Use ordinary Markdown for prose."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "spec": {
                "type": "object",
                "description": (
                    "A smith-ui v1 tree: {root, elements}; each element has type, props, children. "
                    "Use only static standard components such as Card, KeyValue, Table, StatusLine, "
                    "ProgressBar, Heading, Text, and BarChart."
                ),
            },
            "images": {
                "type": "array",
                "description": "Optional local image attachments inside the working directory.",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "alt": {"type": "string"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                    },
                    "required": ["path", "alt"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["spec"],
        "additionalProperties": False,
    },
    "permission_level": "read",
    "approval_policy": "never",
    "side_effect": "none",
    "execution_environment": "host",
}


async def execute(**_: object) -> str:
    """Defensive fallback; the ReAct loop intercepts this presentation tool."""
    return "Error: render_ui must be handled by the execution engine"
