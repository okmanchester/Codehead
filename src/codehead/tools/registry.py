"""Registration and dispatch for tools."""

from collections.abc import Iterable

from codehead.errors import ToolExecutionError
from codehead.models import ToolDefinition
from codehead.tools.base import Tool


class ToolRegistry:
    """Store tools by name and provide definitions for model requests."""

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
