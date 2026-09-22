"""Registration and dispatch for tools."""

from collections.abc import Iterable
from copy import deepcopy

from codehead.errors import ToolExecutionError
from codehead.models import ToolCall, ToolDefinition, ToolResult
from codehead.tools.base import Tool


class ToolRegistry:
    """Store tools by name and dispatch model tool calls."""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        """Register a uniquely named tool."""
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        """Look up a tool by name."""
        try:
            return self._tools[name]
        except KeyError:
            raise ToolExecutionError(f"Unknown tool: {name}") from None

    def definitions(self) -> tuple[ToolDefinition, ...]:
        """Return tool definitions in registration order."""
        return tuple(tool.definition() for tool in self._tools.values())

    def execute(self, tool_call: ToolCall) -> ToolResult:
        """Execute a tool call and associate its output with the call id."""
        tool = self.get(tool_call.name)
        try:
            output = tool.execute(deepcopy(tool_call.arguments))
        except ToolExecutionError:
            raise
        except Exception as exc:
            raise ToolExecutionError(
                f"Tool '{tool_call.name}' failed: {exc}"
            ) from exc

        return ToolResult(tool_call_id=tool_call.id, output=output)
