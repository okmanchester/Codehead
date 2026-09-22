"""Apply one exact text replacement inside the configured workspace."""

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


class EditTool(Tool):
    name = "edit"
    description = "Replace one unique exact text occurrence in a workspace file."
    parameters: ClassVar[JsonObject] = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.expanduser().resolve()

    def execute(self, arguments: Mapping[str, JsonValue]) -> str:
        path_value = require_string_argument(arguments, "path")
        old_text = require_string_argument(arguments, "old_text")
        new_text = require_string_argument(
            arguments,
            "new_text",
            allow_empty=True,
        )
        path = resolve_workspace_path(self.workspace, path_value)

        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ToolExecutionError(f"Failed to edit '{path_value}': {exc}") from exc

        occurrences = content.count(old_text)
        if occurrences == 0:
            raise ToolExecutionError(
                f"Text to replace was not found in '{path_value}'"
            )
        if occurrences > 1:
            raise ToolExecutionError(
                f"Text to replace occurs {occurrences} times in '{path_value}'"
            )

        try:
            path.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ToolExecutionError(f"Failed to edit '{path_value}': {exc}") from exc

        return f"Edited {path.relative_to(self.workspace)}"
