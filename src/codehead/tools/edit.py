"""Placeholder edit tool."""

from collections.abc import Mapping
from typing import Any, ClassVar

from codehead.errors import ToolExecutionError
from codehead.tools.base import Tool


class EditTool(Tool):
    name = "edit"
    description = "Replace exact text in a workspace file."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    }

    def execute(self, arguments: Mapping[str, Any]) -> str:
        raise ToolExecutionError("edit tool is not implemented in Codehead v0")
