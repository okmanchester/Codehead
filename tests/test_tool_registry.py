from collections.abc import Mapping
from threading import Event
from typing import ClassVar, cast

import pytest

from codehead.errors import ToolExecutionError, ToolInternalError
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


def test_unexpected_tool_error_aborts_execution() -> None:
    registry = ToolRegistry([BrokenTool()])

    with pytest.raises(ToolInternalError, match="broken.*boom"):
        ToolRuntime(registry).execute(ToolCall(id="call-1", name="broken"))


def test_rejects_invalid_tool_schema_at_registration() -> None:
    class InvalidTool(EchoTool):
        name = "invalid"
        parameters: ClassVar[JsonObject] = {
            "type": "object",
            "properties": {"text": {"type": "not-a-json-schema-type"}},
        }

    with pytest.raises(ValueError, match="invalid JSON Schema"):
        ToolRegistry([InvalidTool()])


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"text": 7},
        {"text": ""},
        {"text": "abc", "extra": True},
        {"text": "abc", "limit": True},
        {"text": "abc", "limit": 3},
    ],
)
def test_schema_rejects_invalid_arguments_before_tool_runs(
    arguments: JsonObject,
) -> None:
    class CountingTool(EchoTool):
        name = "counting"
        parameters: ClassVar[JsonObject] = {
            "type": "object",
            "properties": {
                "text": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2},
            },
            "required": ["text"],
            "additionalProperties": False,
        }

        def __init__(self) -> None:
            self.calls = 0

        def execute(self, arguments: Mapping[str, JsonValue]) -> str:
            self.calls += 1
            return str(arguments["text"])

    tool = CountingTool()
    result = ToolRuntime(ToolRegistry([tool])).execute(
        ToolCall(id="bad", name="counting", arguments=arguments)
    )
    assert result.outcome == "failed"
    assert "Invalid arguments" in result.output
    assert tool.calls == 0


def test_advertised_schema_and_runtime_share_registration_snapshot() -> None:
    class MutableTool(EchoTool):
        name = "mutable"
        parameters: ClassVar[JsonObject] = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        }

    tool = MutableTool()
    registry = ToolRegistry([tool])
    advertised = registry.definitions()[0]
    tool.parameters["properties"]["text"]["type"] = "integer"
    advertised.parameters["properties"]["text"]["type"] = "boolean"

    assert (
        registry.definitions()[0].parameters["properties"]["text"]["type"] == "string"
    )
    assert (
        ToolRuntime(registry)
        .execute(ToolCall(id="valid", name="mutable", arguments={"text": "hello"}))
        .outcome
        == "succeeded"
    )
    assert (
        ToolRuntime(registry)
        .execute(ToolCall(id="invalid", name="mutable", arguments={"text": 1}))
        .outcome
        == "failed"
    )


def test_cancel_before_validation_skips_invalid_call_without_running() -> None:
    cancel = Event()
    cancel.set()
    result = ToolRuntime(ToolRegistry([EchoTool()])).execute(
        ToolCall(id="cancelled", name="echo", arguments={}), cancel
    )
    assert result.outcome == "skipped"
