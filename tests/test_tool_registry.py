from collections.abc import Mapping
from typing import ClassVar, cast

import pytest

from codehead.errors import ToolExecutionError
from codehead.models import JsonObject, JsonValue, ToolCall, ToolResult
from codehead.tools import Tool, ToolRegistry, ToolRuntime


class EchoTool(Tool):
    name = "echo"
    description = "Return the supplied text."
    parameters: ClassVar[JsonObject] = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def execute(self, arguments: Mapping[str, JsonValue]) -> str:
        return str(arguments["text"])


class BrokenTool(Tool):
    name = "broken"
    description = "Raise a non-domain error."
    parameters: ClassVar[JsonObject] = {
        "type": "object",
        "properties": {},
    }

    def execute(self, arguments: Mapping[str, JsonValue]) -> str:
        raise RuntimeError("boom")


class MutatingTool(Tool):
    name = "mutate"
    description = "Mutate nested arguments."
    parameters: ClassVar[JsonObject] = {
        "type": "object",
        "properties": {"values": {"type": "array"}},
    }

    def execute(self, arguments: Mapping[str, JsonValue]) -> str:
        values = cast(list[JsonValue], arguments["values"])
        values.append("changed")
        return "done"


def test_register_get_definition_and_runtime_execute() -> None:
    tool = EchoTool()
    registry = ToolRegistry([tool])

    assert registry.get("echo") is tool
    assert registry.definitions() == (tool.definition(),)
    assert ToolRuntime(registry).execute(
        ToolCall(id="call-1", name="echo", arguments={"text": "hello"})
    ) == ToolResult(tool_call_id="call-1", output="hello")


def test_execute_uses_a_defensive_arguments_copy() -> None:
    call = ToolCall(
        id="call-1",
        name="mutate",
        arguments={"values": ["original"]},
    )

    ToolRuntime(ToolRegistry([MutatingTool()])).execute(call)

    assert call.arguments == {"values": ["original"]}


def test_duplicate_and_unknown_tools_are_rejected() -> None:
    registry = ToolRegistry([EchoTool()])

    with pytest.raises(ValueError, match="already registered"):
        registry.register(EchoTool())
    with pytest.raises(ToolExecutionError, match="Unknown tool"):
        registry.get("missing")


def test_unexpected_tool_error_becomes_a_result() -> None:
    registry = ToolRegistry([BrokenTool()])

    result = ToolRuntime(registry).execute(ToolCall(id="call-1", name="broken"))

    assert result.is_error
    assert "broken" in result.output and "boom" in result.output
