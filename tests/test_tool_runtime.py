from collections.abc import Mapping
from typing import ClassVar

import pytest

from codehead.errors import ToolExecutionError
from codehead.models import JsonObject, JsonValue, ToolCall
from codehead.tools import Tool, ToolRegistry, ToolRuntime


class ReturningTool(Tool):
    name = "returning"
    description = "Return test text."
    parameters: ClassVar[JsonObject] = {"type": "object", "properties": {}}

    def __init__(self, output: str) -> None:
        self.output = output

    def execute(self, arguments: Mapping[str, JsonValue]) -> str:
        return self.output


class FailingTool(ReturningTool):
    name = "failing"

    def __init__(self, error: Exception) -> None:
        self.error = error

    def execute(self, arguments: Mapping[str, JsonValue]) -> str:
        raise self.error


@pytest.mark.parametrize(
    ("text", "truncated"),
    [("界" * 30 + "x" * 10, False), ("界" * 31 + "x" * 10, True)],
)
def test_runtime_uses_utf8_bytes_and_preserves_head_tail(
    text: str, truncated: bool
) -> None:
    runtime = ToolRuntime(ToolRegistry([ReturningTool(text)]), max_output_bytes=100)

    result = runtime.execute(ToolCall(id="call", name="returning"))

    assert not result.is_error
    assert len(result.output.encode("utf-8")) <= 100
    assert ("[output truncated]" in result.output) is truncated
    if truncated:
        assert result.output.startswith("界")
        assert result.output.endswith("x" * 10)
    else:
        assert result.output == text


@pytest.mark.parametrize(
    "error",
    [
        ToolExecutionError("known " + "x" * 200 + " tail"),
        RuntimeError("boom " + "x" * 200 + " tail"),
    ],
)
def test_runtime_limits_known_and_unexpected_error_results(error: Exception) -> None:
    runtime = ToolRuntime(ToolRegistry([FailingTool(error)]), max_output_bytes=100)

    result = runtime.execute(ToolCall(id="call", name="failing"))

    assert result.is_error
    assert result.tool_call_id == "call"
    assert len(result.output.encode("utf-8")) <= 100
    assert result.output.endswith(" tail")
    assert "[output truncated]" in result.output


def test_unknown_tool_and_non_text_output_become_errors() -> None:
    registry = ToolRegistry([ReturningTool("ok")])
    unknown = ToolRuntime(registry).execute(ToolCall(id="missing", name="missing"))
    assert unknown.is_error and "Unknown tool" in unknown.output

    tool = registry.get("returning")
    tool.execute = lambda arguments: None  # type: ignore[method-assign,return-value]
    invalid = ToolRuntime(registry).execute(ToolCall(id="invalid", name="returning"))
    assert invalid.is_error and "non-text output" in invalid.output


def test_runtime_does_not_swallow_process_level_interrupt() -> None:
    class InterruptedTool(ReturningTool):
        def execute(self, arguments: Mapping[str, JsonValue]) -> str:
            raise KeyboardInterrupt

    runtime = ToolRuntime(ToolRegistry([InterruptedTool("unused")]))
    with pytest.raises(KeyboardInterrupt):
        runtime.execute(ToolCall(id="call", name="returning"))


def test_runtime_rejects_impossibly_small_limit() -> None:
    with pytest.raises(ValueError, match="too small"):
        ToolRuntime(ToolRegistry(), max_output_bytes=10)


@pytest.mark.parametrize("limit", [None, True, False, 50.5, "100"])
def test_runtime_rejects_non_integer_limit(limit: object) -> None:
    with pytest.raises(TypeError, match="max_output_bytes must be an integer"):
        ToolRuntime(ToolRegistry(), max_output_bytes=limit)  # type: ignore[arg-type]


@pytest.mark.parametrize("registry", [None, {}, "registry"])
def test_runtime_rejects_invalid_registry(registry: object) -> None:
    with pytest.raises(TypeError, match="registry must be a ToolRegistry"):
        ToolRuntime(registry)  # type: ignore[arg-type]


@pytest.mark.parametrize("call", [None, {}, "call"])
def test_runtime_rejects_invalid_call(call: object) -> None:
    runtime = ToolRuntime(ToolRegistry())
    with pytest.raises(TypeError, match="call must be a ToolCall"):
        runtime.execute(call)  # type: ignore[arg-type]
