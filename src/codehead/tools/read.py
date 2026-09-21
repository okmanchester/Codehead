"""Placeholder read tool."""

from collections.abc import Mapping
from typing import Any, ClassVar

from codehead.errors import ToolExecutionError
from codehead.tools.base import Tool


class ReadTool(Tool):
    name = "read"
    description = "Read text from a file in the workspace."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }

    def execute(self, arguments: Mapping[str, Any]) -> str:
        raise ToolExecutionError("read tool is not implemented in Codehead v0")
