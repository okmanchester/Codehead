"""Placeholder bash tool."""

from collections.abc import Mapping
from typing import Any, ClassVar

from codehead.errors import ToolExecutionError
from codehead.tools.base import Tool


class BashTool(Tool):
    name = "bash"
    description = "Run a shell command in the workspace."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
        "additionalProperties": False,
    }

    def execute(self, arguments: Mapping[str, Any]) -> str:
        raise ToolExecutionError("bash tool is not implemented in Codehead v0")
