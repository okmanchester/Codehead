"""Read bounded, numbered pages of UTF-8 text from the workspace."""

import codecs
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO, ClassVar

from codehead.errors import ToolExecutionError
from codehead.models import JsonObject, JsonValue
from codehead.tools.base import Tool, require_string_argument, resolve_workspace_path

READ_MAX_BYTES = 40 * 1024
LINE_PREVIEW_BYTES = 2 * 1024
DEFAULT_LIMIT = 200
MAX_LIMIT = 500
_FOOTER_RESERVE = 160


def _positive_int(
    arguments: Mapping[str, JsonValue],
    name: str,
    default: int,
    maximum: int | None = None,
) -> int:
    value = arguments.get(name, default)
    if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
        range_text = f" between 1 and {maximum}" if maximum is not None else " >= 1"
        raise ToolExecutionError(f"'{name}' must be an integer{range_text}")
    return value


def _read_line(file: BinaryIO) -> tuple[str, bool] | None:
    """Consume one whole line while retaining at most its first 2 KiB."""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    prefix = bytearray()
    total_bytes = 0
    line_ending_bytes = 0
    previous_byte: int | None = None
    seen = False
    while True:
        chunk = file.readline(8192)
        if not chunk:
            break
        seen = True
        total_bytes += len(chunk)
        if b"\0" in chunk:
            raise ToolExecutionError(
                "Binary file: NUL byte found; read supports UTF-8 text only"
            )
        decoder.decode(chunk, final=False)
        remaining = LINE_PREVIEW_BYTES - len(prefix)
        prefix.extend(chunk[:remaining])
        if chunk.endswith(b"\n"):
            cr_before_newline = chunk.endswith(b"\r\n") or (
                chunk == b"\n" and previous_byte == ord("\r")
            )
            line_ending_bytes = 2 if cr_before_newline else 1
            break
        previous_byte = chunk[-1]
    if not seen:
        return None
    decoder.decode(b"", final=True)
    truncated = total_bytes - line_ending_bytes > LINE_PREVIEW_BYTES
    # The preview may end inside a code point; the complete line was validated.
    text = prefix.decode("utf-8", errors="ignore" if truncated else "strict")
    if line_ending_bytes:
        text = text.removesuffix("\n")
        if line_ending_bytes == 2:
            text = text.removesuffix("\r")
    return text, truncated


class ReadTool(Tool):
    name = "read"
    description = (
        "Read numbered UTF-8 lines from a workspace file. offset is a 1-based line "
        "number; limit defaults to 200 (max 500). Output may be incomplete; "
        "follow next_offset and limit to continue. Long lines show a 2 KiB prefix."
    )
    parameters: ClassVar[JsonObject] = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.expanduser().resolve()

    def execute(self, arguments: Mapping[str, JsonValue]) -> str:
        path_value = require_string_argument(arguments, "path")
        offset = _positive_int(arguments, "offset", 1)
        limit = _positive_int(arguments, "limit", DEFAULT_LIMIT, MAX_LIMIT)
        path = resolve_workspace_path(self.workspace, path_value)
        try:
            if path.is_dir():
                raise ToolExecutionError(
                    f"Is a directory: '{path_value}'; read requires a file"
                )
            with path.open("rb") as file:
                for _ in range(offset - 1):
                    if _read_line(file) is None:
                        return f"No lines at or after offset={offset}; end of file."

                lines: list[str] = []
                size = 0
                next_offset: int | None = None
                footer_reserve = max(
                    _FOOTER_RESERVE,
                    len(f"next_offset={offset + limit}, limit={limit}".encode()) + 100,
                )
                for number in range(offset, offset + limit):
                    line = _read_line(file)
                    if line is None:
                        break
                    preview, truncated = line
                    suffix = (
                        " [line truncated: only first 2 KiB shown]" if truncated else ""
                    )
                    entry = f"{number}: {preview}{suffix}\n"
                    entry_size = len(entry.encode("utf-8"))
                    if size + entry_size + footer_reserve > READ_MAX_BYTES:
                        next_offset = number
                        break
                    lines.append(entry)
                    size += entry_size
                else:
                    if file.read(1):
                        next_offset = offset + limit

            if next_offset is not None:
                lines.append(
                    f"[output truncated; more lines available. "
                    f"Continue read on the same path with next_offset={next_offset}, limit={limit}]"
                )
            return (
                "".join(lines)
                if lines
                else f"No lines at or after offset={offset}; end of file."
            )
        except FileNotFoundError as exc:
            raise ToolExecutionError(f"File not found: '{path_value}'") from exc
        except UnicodeError as exc:
            raise ToolExecutionError(f"Invalid UTF-8 in '{path_value}': {exc}") from exc
        except OSError as exc:
            raise ToolExecutionError(f"Could not read '{path_value}': {exc}") from exc
