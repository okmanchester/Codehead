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
    description = (
        "Replace one unique exact text occurrence in a workspace file. "
        "old_text and new_text must differ. Multiple matches return line/column "
        "candidates without changing the file."
    )
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
        if old_text == new_text:
            raise ToolExecutionError(
                "old_text and new_text are identical; no edit was made"
            )
        path = resolve_workspace_path(self.workspace, path_value)

        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ToolExecutionError(f"Failed to edit '{path_value}': {exc}") from exc

        occurrences = content.count(old_text)
        if occurrences == 0:
            first_line = old_text.split("\n", 1)[0].removesuffix("\r")
            preview = first_line[:120]
            if len(first_line) > 120:
                preview = preview[:119] + "…"
            raise ToolExecutionError(
                f"Text to replace was not found in '{path_value}'. "
                f"old_text starts with {preview!r}; use read or grep to locate the exact text."
            )
        if occurrences > 1:
            candidates = _match_candidates(content, old_text, min(occurrences, 5))
            remainder = occurrences - len(candidates)
            lines = [
                (
                    f"Text to replace occurs {occurrences} times in '{path_value}'. "
                    "Candidates (1-based line:column):"
                )
            ]
            lines.extend(
                f"  {line}:{column}: {context!r}"
                for line, column, context in candidates
            )
            if remainder:
                lines.append(f"  ... and {remainder} more matches")
            lines.append("Use a longer old_text to target one occurrence.")
            raise ToolExecutionError("\n".join(lines))

        try:
            path.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ToolExecutionError(f"Failed to edit '{path_value}': {exc}") from exc

        return f"Edited {path.relative_to(self.workspace)}"


def _match_candidates(
    content: str, old_text: str, limit: int
) -> list[tuple[int, int, str]]:
    """Return bounded diagnostics for non-overlapping exact matches."""
    candidates: list[tuple[int, int, str]] = []
    search_from = 0
    for _ in range(limit):
        position = content.find(old_text, search_from)
        if position < 0:
            break
        line_start = content.rfind("\n", 0, position) + 1
        line_end = content.find("\n", position)
        if line_end < 0:
            line_end = len(content)
        line = content.count("\n", 0, position) + 1
        column = position - line_start + 1
        window_start = max(line_start, position - 40)
        window_end = min(line_end, window_start + 118)
        context = content[window_start:window_end]
        if window_start > line_start:
            context = "…" + context
        if window_end < line_end:
            context += "…"
        candidates.append((line, column, context))
        search_from = position + len(old_text)
    return candidates
