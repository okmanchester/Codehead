"""Run shell commands from the configured workspace."""

import os
import platform
import signal
import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO, ClassVar

from codehead.errors import ToolExecutionError
from codehead.models import JsonObject, JsonValue
from codehead.tools.base import Tool, require_string_argument
from codehead.tools.output import TRUNCATION_MARKER

BASH_STREAM_MAX_BYTES = 20 * 1024


class BashTool(Tool):
    name = "bash"
    description = (
        "Run a shell command from the workspace "
        "(PowerShell on Windows, Bash on Unix). Large stdout/stderr are truncated."
    )
    parameters: ClassVar[JsonObject] = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Path, timeout_seconds: float = 60) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self.workspace = workspace.expanduser().resolve()
        self.timeout_seconds = timeout_seconds

    def execute(self, arguments: Mapping[str, JsonValue]) -> str:
        command = require_string_argument(arguments, "command")
        environment = os.environ.copy()
        environment.pop("DEEPSEEK_API_KEY", None)

        try:
            popen_options = (
                {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                if os.name == "nt"
                else {"start_new_session": True}
            )
            process = subprocess.Popen(
                _shell_command(command),
                cwd=self.workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                **popen_options,
            )
        except OSError as exc:
            raise ToolExecutionError(f"Failed to start command: {exc}") from exc

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

        timed_out = False
        try:
            try:
                returncode = process.wait(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_tree(process)
                returncode = process.wait()
        finally:
            if process.poll() is None:
                _terminate_process_tree(process)
                process.wait()
            for reader in readers:
                reader.join(timeout=2)
        if any(reader.is_alive() for reader in readers):
            raise ToolExecutionError("Failed to finish collecting command output")
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
        if timed_out or returncode != 0:
            raise ToolExecutionError(output)
        return output


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Stop descendants that may still hold the output pipes open."""
    if os.name == "nt":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
            process.wait(timeout=1)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()


def _shell_command(command: str) -> list[str]:
    if platform.system() == "Windows":
        wrapped_command = (
            f"& {{ {command} }}\n"
            "$command_succeeded = $?\n"
            "$native_exit_code = $LASTEXITCODE\n"
            "if ($null -ne $native_exit_code) { exit $native_exit_code }\n"
            "if (-not $command_succeeded) { exit 1 }\n"
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
