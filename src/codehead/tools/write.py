"""Placeholder write tool."""

from collections.abc import Mapping
from typing import Any, ClassVar

from codehead.errors import ToolExecutionError
from codehead.tools.base import Tool


class WriteTool(Tool):
    name = "write"
    description = "Write text to a file in the workspace."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def execute(self, arguments: Mapping[str, Any]) -> str:
        raise ToolExecutionError("write tool is not implemented in Codehead v0")
