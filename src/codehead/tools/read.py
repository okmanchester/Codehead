"""Read text files from the configured workspace."""

import codecs
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
from codehead.tools.output import TRUNCATION_MARKER

READ_MAX_BYTES = 40 * 1024


class ReadTool(Tool):
    name = "read"
    description = (
        "Read the beginning of a UTF-8 text file inside the workspace. "
        "Large files are truncated."
    )
    parameters: ClassVar[JsonObject] = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.expanduser().resolve()

    def execute(self, arguments: Mapping[str, JsonValue]) -> str:
        path_value = require_string_argument(arguments, "path")
        path = resolve_workspace_path(self.workspace, path_value)
        try:
            with path.open("rb") as file:
                data = file.read(READ_MAX_BYTES + 1)
            truncated = len(data) > READ_MAX_BYTES
            decoder = codecs.getincrementaldecoder("utf-8")("strict")
            content = decoder.decode(data[:READ_MAX_BYTES], final=not truncated)
            if truncated:
                return content + TRUNCATION_MARKER
            return content
        except (OSError, UnicodeError) as exc:
            raise ToolExecutionError(f"Failed to read '{path_value}': {exc}") from exc
