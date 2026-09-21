from collections.abc import Mapping
from typing import Any, ClassVar

import pytest

from codehead.errors import ToolExecutionError
from codehead.session import ToolCall, ToolResult
from codehead.tools import Tool, ToolRegistry


class EchoTool(Tool):
    name = "echo"
    description = "Return the supplied text."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def execute(self, arguments: Mapping[str, Any]) -> str:
        return str(arguments["text"])


class BrokenTool(Tool):
    name = "broken"
    description = "Raise a non-domain error."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
    }

    def execute(self, arguments: Mapping[str, Any]) -> str:
        raise RuntimeError("boom")


def test_register_get_schema_and_execute() -> None:
    tool = EchoTool()
    registry = ToolRegistry([tool])

    assert registry.get("echo") is tool
    assert registry.schemas() == [tool.schema()]
    assert registry.execute(
        ToolCall(id="call-1", name="echo", arguments={"text": "hello"})
    ) == ToolResult(tool_call_id="call-1", output="hello")


def test_duplicate_and_unknown_tools_are_rejected() -> None:
    registry = ToolRegistry([EchoTool()])

    with pytest.raises(ValueError, match="already registered"):
        registry.register(EchoTool())
    with pytest.raises(ToolExecutionError, match="Unknown tool"):
        registry.get("missing")


def test_unexpected_tool_error_is_wrapped() -> None:
    registry = ToolRegistry([BrokenTool()])

    with pytest.raises(ToolExecutionError, match="broken.*boom"):
        registry.execute(ToolCall(id="call-1", name="broken"))
