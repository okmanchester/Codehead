"""Common interface for Codehead tools."""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import ClassVar

from codehead.errors import ToolExecutionError
from codehead.models import JsonObject, JsonValue, ToolDefinition


class Tool(ABC):
    """A callable capability exposed to the model."""

    name: ClassVar[str]
    description: ClassVar[str]
    parameters: ClassVar[JsonObject]

    def definition(self) -> ToolDefinition:
        """Return the provider-independent definition for this tool."""
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=deepcopy(self.parameters),
        )

    @abstractmethod
    def execute(self, arguments: Mapping[str, JsonValue]) -> str:
        """Execute the tool and return textual output."""


def require_string_argument(
    arguments: Mapping[str, JsonValue],
    name: str,
    *,
    allow_empty: bool = False,
) -> str:
    """Return one required string argument or raise a domain error."""
    value = arguments.get(name)
    if not isinstance(value, str):
        raise ToolExecutionError(f"Tool argument '{name}' must be a string")
    if not allow_empty and not value:
        raise ToolExecutionError(f"Tool argument '{name}' must not be empty")
    return value


def resolve_workspace_path(workspace: Path, path_value: str) -> Path:
    """Resolve a path and reject anything outside the configured workspace."""
    candidate = Path(path_value).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate

    resolved = candidate.resolve()
    if not resolved.is_relative_to(workspace):
        raise ToolExecutionError(f"Path is outside the workspace: {path_value}")
    return resolved
