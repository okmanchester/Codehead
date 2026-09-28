"""Apply one exact text replacement inside the configured workspace."""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar

from codehead.errors import ToolExecutionError
from codehead.models import JsonObject, JsonValue
from codehead.tools._atomic import replace_bytes
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
            "path": {"type": "string", "minLength": 1},
            "old_text": {"type": "string", "minLength": 1},
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
            content = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise ToolExecutionError(f"Failed to edit '{path_value}': {exc}") from exc

        # ReadTool displays CRLF as LF. Match that logical text while retaining
        # the original bytes of everything outside the selected occurrence.
        pattern = re.compile(
            r"\r?\n".join(
                re.escape(part) for part in old_text.replace("\r\n", "\n").split("\n")
            )
        )
        positions: list[int] = []
        occurrences = 0
        match_span: tuple[int, int] | None = None
        for match in pattern.finditer(content):
            occurrences += 1
            if len(positions) < 5:
                positions.append(match.start())
            if occurrences == 1:
                match_span = match.span()
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
            candidates = _match_candidates(content, positions)
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
            assert match_span is not None
            start, end = match_span
            newline = _replacement_newline(content, start, end)
            replacement = re.sub(r"(?<!\r)\n", lambda _: newline, new_text)
            updated = content[:start] + replacement + content[end:]
            replace_bytes(path, updated.encode("utf-8"))
        except (OSError, UnicodeError) as exc:
            raise ToolExecutionError(f"Failed to edit '{path_value}': {exc}") from exc

        return f"Edited {path.relative_to(self.workspace)}"


def _match_candidates(content: str, positions: list[int]) -> list[tuple[int, int, str]]:
    """Return bounded diagnostics for non-overlapping exact matches."""
    candidates: list[tuple[int, int, str]] = []
    for position in positions:
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
    return candidates


def _replacement_newline(content: str, start: int, end: int) -> str:
    """Use the match's line ending, or the closest existing line ending."""
    within = content.find("\n", start, end)
    if within >= 0:
        return "\r\n" if within > 0 and content[within - 1] == "\r" else "\n"
    previous = content.rfind("\n", 0, start)
    following = content.find("\n", end)
    if previous < 0 and following < 0:
        return "\n"
    if following < 0 or (previous >= 0 and start - previous <= following - end):
        position = previous
    else:
        position = following
    return "\r\n" if position > 0 and content[position - 1] == "\r" else "\n"
