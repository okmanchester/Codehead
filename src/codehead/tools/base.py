"""Common interface for Codehead tools."""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, ClassVar

type ToolSchema = dict[str, Any]


class Tool(ABC):
    """A callable capability exposed to the model."""

    name: ClassVar[str]
    description: ClassVar[str]
    parameters: ClassVar[ToolSchema]

    def schema(self) -> ToolSchema:
        """Return the provider-independent function schema for this tool."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    @abstractmethod
    def execute(self, arguments: Mapping[str, Any]) -> str:
        """Execute the tool and return textual output."""
