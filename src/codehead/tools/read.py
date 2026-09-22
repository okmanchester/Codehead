"""Read text files from the configured workspace."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from codehead.errors import ToolExecutionError
from codehead.tools.base import (
    Tool,
    require_string_argument,
    resolve_workspace_path,
)


class ReadTool(Tool):
    name = "read"
    description = "Read an entire UTF-8 text file inside the workspace."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.expanduser().resolve()

    def execute(self, arguments: Mapping[str, Any]) -> str:
        path_value = require_string_argument(arguments, "path")
        path = resolve_workspace_path(self.workspace, path_value)
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ToolExecutionError(f"Failed to read '{path_value}': {exc}") from exc
