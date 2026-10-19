"""Run shell commands from the configured workspace."""

import os
import platform
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO, ClassVar

from codehead.errors import ToolCancelled, ToolCleanupError, ToolExecutionError
from codehead.models import JsonObject, JsonValue
from codehead.tools.base import Tool, require_string_argument, resolve_workspace_path
from codehead.tools.output import TRUNCATION_MARKER
from codehead.tools.process import ManagedProcess

BASH_STREAM_MAX_BYTES = 20 * 1024


class BashTool(Tool):
    name = "bash"
    description = (
        "Run a shell command (PowerShell on Windows, Bash on Unix). "
        "Optional workdir selects a workspace directory for this call only; "
        "default is the workspace root. Large stdout/stderr are truncated."
    )
    parameters: ClassVar[JsonObject] = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "minLength": 1},
            "workdir": {
                "type": "string",
                "minLength": 1,
                "description": "Working directory inside the workspace for this command only.",
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Path, timeout_seconds: float = 60) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self.workspace = workspace.expanduser().resolve()
        self.timeout_seconds = timeout_seconds

    def execute(self, arguments: Mapping[str, JsonValue]) -> str:
        return self.execute_cancellable(arguments, threading.Event())

    def execute_cancellable(
        self, arguments: Mapping[str, JsonValue], cancel: threading.Event
    ) -> str:
        command = require_string_argument(arguments, "command")
        workdir_value = arguments.get("workdir", ".")
        if not isinstance(workdir_value, str) or not workdir_value:
            raise ToolExecutionError(
                "Tool argument 'workdir' must be a non-empty string"
            )
        workdir = resolve_workspace_path(self.workspace, workdir_value)
        try:
            if not workdir.exists():
                raise ToolExecutionError(
                    f"Working directory does not exist: '{workdir_value}'"
                )
            if not workdir.is_dir():
                raise ToolExecutionError(
                    f"Working directory is not a directory: '{workdir_value}'"
                )
        except OSError as exc:
            raise ToolExecutionError(
                f"Cannot inspect working directory '{workdir_value}': {exc}"
            ) from exc
        environment = os.environ.copy()
        environment.pop("DEEPSEEK_API_KEY", None)
        if platform.system() == "Windows":
            # PowerShell 7 module paths inherited through Python can shadow the
            # Windows PowerShell modules. Let powershell.exe build its defaults.
            environment = {
                key: value
                for key, value in environment.items()
                if key.upper() != "PSMODULEPATH"
            }

        managed = ManagedProcess(
            _shell_command(command),
            cwd=workdir,
            cancel=cancel,
            timeout=self.timeout_seconds,
            env=environment,
        )
        process = managed.process

        assert process.stdout is not None and process.stderr is not None
        stdout = _BoundedBytes(BASH_STREAM_MAX_BYTES)
        stderr = _BoundedBytes(BASH_STREAM_MAX_BYTES)
        read_errors: list[OSError] = []
        readers = [
            threading.Thread(
                target=_drain, args=(process.stdout, stdout, read_errors), daemon=True
            ),
            threading.Thread(
                target=_drain, args=(process.stderr, stderr, read_errors), daemon=True
            ),
        ]
        for reader in readers:
            reader.start()

        try:
            returncode = managed.wait()
        finally:
            try:
                managed.close()
            finally:
                for reader in readers:
                    reader.join(timeout=5)
        timed_out = managed.timed_out
        if any(reader.is_alive() for reader in readers):
            raise ToolCleanupError("Failed to finish collecting command output")
        if read_errors:
            raise ToolExecutionError(
                f"Failed to collect command output: {read_errors[0]}"
            )

        heading = (
            f"Command timed out after {self.timeout_seconds:g} seconds"
            if timed_out
            else f"Exit code: {returncode}"
        )
        output = _format_output(
            heading,
            stdout.render(),
            stderr.render(),
        )
        if managed.cancelled:
            raise ToolCancelled(
                "Cancelled: process tree stopped; partial side effects may have occurred\n"
                + output
            )
        if timed_out or returncode != 0:
            raise ToolExecutionError(output)
        return output


def _shell_command(command: str) -> list[str]:
    if platform.system() == "Windows":
        wrapped_command = (
            "$OutputEncoding = [Console]::InputEncoding = "
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
            f"& {{ {command}\n"
            "$global:__codehead_command_succeeded = $?\n"
            "$global:__codehead_native_exit_code = $LASTEXITCODE\n"
            "} | Out-Default\n"
            "if ($null -ne $__codehead_native_exit_code) { exit $__codehead_native_exit_code }\n"
            "if (-not $__codehead_command_succeeded) { exit 1 }\n"
            "exit 0"
        )
        return [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            wrapped_command,
        ]
    return ["/bin/bash", "-lc", command]


class _BoundedBytes:
    """Retain only the first and last bytes of a continuously drained pipe."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        head_limit = self.max_bytes // 2
        missing = max(0, head_limit - len(self.head))
        self.head.extend(chunk[:missing])
        self.tail.extend(chunk[missing:])
        tail_limit = self.max_bytes - head_limit
        if len(self.tail) > tail_limit:
            del self.tail[:-tail_limit]

    def render(self) -> str:
        if self.total <= self.max_bytes:
            return (self.head + self.tail).decode("utf-8", errors="replace")
        return (
            self.head.decode("utf-8", errors="ignore")
            + TRUNCATION_MARKER
            + self.tail.decode("utf-8", errors="ignore")
        )


def _drain(pipe: BinaryIO, output: _BoundedBytes, errors: list[OSError]) -> None:
    try:
        while chunk := pipe.read(8192):
            output.add(chunk)
    except OSError as exc:
        errors.append(exc)
    finally:
        pipe.close()


def _format_output(heading: str, stdout: str, stderr: str) -> str:
    sections = [heading]
    if stdout:
        sections.extend(("stdout:", stdout.rstrip()))
    if stderr:
        sections.extend(("stderr:", stderr.rstrip()))
    return "\n".join(sections)
