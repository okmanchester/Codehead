"""Write text files inside the configured workspace."""

from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar

from codehead.errors import ToolExecutionError
from codehead.models import JsonObject, JsonValue
from codehead.tools.base import (
    Tool,
    require_string_argument,
    resolve_workspace_path,
)


class WriteTool(Tool):
    name = "write"
    description = "Create or overwrite a UTF-8 text file inside the workspace."
    parameters: ClassVar[JsonObject] = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.expanduser().resolve()

    def execute(self, arguments: Mapping[str, JsonValue]) -> str:
        path_value = require_string_argument(arguments, "path")
        content = require_string_argument(arguments, "content", allow_empty=True)
        path = resolve_workspace_path(self.workspace, path_value)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ToolExecutionError(
                f"Failed to write '{path_value}': {exc}"
            ) from exc

        relative_path = path.relative_to(self.workspace)
        return f"Wrote {len(content)} characters to {relative_path}"
