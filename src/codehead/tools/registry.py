"""Registration and dispatch for tools."""

from collections.abc import Iterable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from codehead.errors import ToolExecutionError
from codehead.models import JsonValue, ToolDefinition
from codehead.tools.base import Tool


class ToolRegistry:
    """Store tools by name and provide definitions for model requests."""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        self._definitions: dict[str, ToolDefinition] = {}
        self._validators: dict[str, Draft202012Validator] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        """Register a uniquely named tool."""
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        definition = tool.definition()
        try:
            Draft202012Validator.check_schema(definition.parameters)
        except SchemaError as exc:
            raise ValueError(f"Tool '{tool.name}' has an invalid JSON Schema") from exc
        self._tools[tool.name] = tool
        self._definitions[tool.name] = definition
        self._validators[tool.name] = Draft202012Validator(definition.parameters)

    def get(self, name: str) -> Tool:
        """Look up a tool by name."""
        try:
            return self._tools[name]
        except KeyError:
            raise ToolExecutionError(f"Unknown tool: {name}") from None

    def definitions(self) -> tuple[ToolDefinition, ...]:
        """Return tool definitions in registration order."""
        return tuple(
            definition.model_copy(deep=True)
            for definition in self._definitions.values()
        )

    def validate_arguments(self, name: str, arguments: dict[str, JsonValue]) -> None:
        """Validate an invocation against the advertised registration snapshot."""
        self.get(name)
        try:
            self._validators[name].validate(arguments)
        except ValidationError as exc:
            location = ".".join(str(part) for part in exc.path) or "arguments"
            raise ToolExecutionError(
                f"Invalid arguments for tool '{name}' at {location}: {exc.message}"
            ) from exc
