"""Bounded workspace search backed by ripgrep."""

import shutil
import subprocess
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import BinaryIO, ClassVar

from codehead.errors import ToolExecutionError
from codehead.models import JsonObject, JsonValue
from codehead.tools.base import Tool, require_string_argument, resolve_workspace_path

SEARCH_MAX_BYTES = 40 * 1024
SEARCH_TIMEOUT_SECONDS = 15
MAX_RESULTS = 200
_FOOTER_RESERVE = 160


def find_ripgrep() -> str:
    executable = shutil.which("rg")
    if executable is None:
        raise ValueError(
            "ripgrep (rg) is required for glob and grep; install it and add rg to PATH"
        )
    return executable


def _limit(arguments: Mapping[str, JsonValue]) -> int:
    value = arguments.get("limit", 100)
    if type(value) is not int or not 1 <= value <= MAX_RESULTS:
        raise ToolExecutionError("'limit' must be an integer between 1 and 200")
    return value


def _records(stream: BinaryIO, *, grep: bool) -> Iterator[bytes]:
    """Read NUL paths, or NUL-path/newline-content pairs, in bounded chunks."""
    pending = bytearray()
    path: bytes | None = None
    while chunk := stream.read1(4096):
        pending.extend(chunk)
        while True:
            separator = b"\0" if path is None else b"\n"
            index = pending.find(separator)
            if index < 0:
                break
            part = bytes(pending[:index])
            del pending[: index + 1]
            if not grep:
                yield part
            elif path is None:
                path = part
            else:
                yield path + b"\0" + part
                path = None
        if len(pending) > SEARCH_MAX_BYTES:
            raise ToolExecutionError(
                "Search result path or line is too long; narrow the query"
            )


class _SearchTool(Tool):
    def __init__(self, workspace: Path, rg_executable: str | None = None) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.rg_executable = rg_executable or find_ripgrep()

    def _path(self, arguments: Mapping[str, JsonValue]) -> str:
        value = arguments.get("path", ".")
        if not isinstance(value, str) or not value:
            raise ToolExecutionError("Tool argument 'path' must be a non-empty string")
        resolved = resolve_workspace_path(self.workspace, value)
        if not resolved.exists():
            raise ToolExecutionError(f"Search path not found: '{value}'")
        relative = resolved.relative_to(self.workspace).as_posix()
        return relative or "."

    def _run(self, args: list[str], *, grep: bool, limit: int) -> str:
        command = [self.rg_executable, "--no-config", *args]
        try:
            process = subprocess.Popen(
                command,
                cwd=self.workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise ToolExecutionError(f"Could not start ripgrep: {exc}") from exc

        assert process.stdout is not None
        assert process.stderr is not None
        stderr = bytearray()

        def drain_stderr() -> None:
            while chunk := process.stderr.read(4096):
                if len(stderr) < 4096:
                    stderr.extend(chunk[: 4096 - len(stderr)])

        worker = threading.Thread(target=drain_stderr, daemon=True)
        worker.start()
        timed_out = threading.Event()

        def timeout() -> None:
            if process.poll() is None:
                timed_out.set()
                process.kill()

        timer = threading.Timer(SEARCH_TIMEOUT_SECONDS, timeout)
        timer.start()
        entries: list[str] = []
        size = 0
        capped = False
        try:
            for record in _records(process.stdout, grep=grep):
                if len(entries) >= limit:
                    capped = True
                    break
                if grep:
                    raw_path, details = record.split(b"\0", 1)
                    # --vimgrep emits line:column:preview; column is not needed.
                    line, _, rest = details.partition(b":")
                    _, _, preview = rest.partition(b":")
                    preview_text = preview.removesuffix(b"\r").decode(
                        "utf-8", errors="replace"
                    )
                    entry = (
                        f"{self._relative(raw_path)}:"
                        f"{line.decode('ascii', errors='replace')}:{preview_text}\n"
                    )
                else:
                    entry = f"{self._relative(record)}\n"
                entry_size = len(entry.encode("utf-8"))
                if size + entry_size + _FOOTER_RESERVE > SEARCH_MAX_BYTES:
                    capped = True
                    break
                entries.append(entry)
                size += entry_size
            if capped and process.poll() is None:
                process.kill()
            return_code = process.wait()
            worker.join()
            if timed_out.is_set():
                raise ToolExecutionError(
                    "ripgrep search timed out after 15 seconds; narrow the query"
                )
            if not capped and return_code not in (0, 1):
                detail = stderr.decode("utf-8", errors="replace").strip()
                raise ToolExecutionError(
                    f"ripgrep search failed: {detail or f'exit code {return_code}'}"
                )
            if capped:
                entries.append(
                    "[results truncated; narrow the path or pattern to find more]"
                )
            return "".join(entries) if entries else "No matches."
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()
            worker.join()

    def _relative(self, raw: bytes) -> str:
        path = Path(raw.decode("utf-8", errors="replace"))
        if not path.is_absolute():
            path = self.workspace / path
        try:
            return path.resolve().relative_to(self.workspace).as_posix()
        except ValueError as exc:
            raise ToolExecutionError(
                "ripgrep returned a path outside the workspace"
            ) from exc


class GlobTool(_SearchTool):
    name = "glob"
    description = (
        "Find workspace files using a ripgrep glob pattern. Returns relative paths; "
        "respects default ignore rules unless an explicit glob overrides them. "
        "Narrow the path or pattern if results are truncated."
    )
    parameters: ClassVar[JsonObject] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }

    def execute(self, arguments: Mapping[str, JsonValue]) -> str:
        pattern = require_string_argument(arguments, "pattern")
        path = self._path(arguments)
        return self._run(
            ["--files", "-0", "-g", pattern, "--", path],
            grep=False,
            limit=_limit(arguments),
        )


class GrepTool(_SearchTool):
    name = "grep"
    description = (
        "Search UTF-8 text lines with a case-sensitive ripgrep regex. Returns "
        "workspace-relative path:line:preview (preview capped at 300 bytes). "
        "Optionally filter files with glob. Narrow the query if results are truncated."
    )
    parameters: ClassVar[JsonObject] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
            "glob": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }

    def execute(self, arguments: Mapping[str, JsonValue]) -> str:
        pattern = require_string_argument(arguments, "pattern")
        path = self._path(arguments)
        file_glob = arguments.get("glob")
        if file_glob is not None and (not isinstance(file_glob, str) or not file_glob):
            raise ToolExecutionError("Tool argument 'glob' must be a non-empty string")
        args = ["--vimgrep", "--null", "-M", "300", "--max-columns-preview"]
        if file_glob is not None:
            args.extend(["-g", file_glob])
        args.extend(["-e", pattern, "--", path])
        return self._run(args, grep=True, limit=_limit(arguments))
