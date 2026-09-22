"""Run shell commands from the configured workspace."""

import os
import platform
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from codehead.errors import ToolExecutionError
from codehead.tools.base import Tool, require_string_argument


class BashTool(Tool):
    name = "bash"
    description = (
        "Run a shell command from the workspace "
        "(PowerShell on Windows, Bash on Unix)."
    )
    parameters: ClassVar[dict[str, Any]] = {
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

    def execute(self, arguments: Mapping[str, Any]) -> str:
        command = require_string_argument(arguments, "command")
        environment = os.environ.copy()
        environment.pop("DEEPSEEK_API_KEY", None)

        try:
            completed = subprocess.run(
                _shell_command(command),
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolExecutionError(
                f"Command timed out after {self.timeout_seconds:g} seconds"
            ) from exc
        except OSError as exc:
            raise ToolExecutionError(f"Failed to start command: {exc}") from exc

        output = _format_output(
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )
        if completed.returncode != 0:
            raise ToolExecutionError(output)
        return output


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


def _format_output(returncode: int, stdout: str, stderr: str) -> str:
    sections = [f"Exit code: {returncode}"]
    if stdout:
        sections.extend(("stdout:", stdout.rstrip()))
    if stderr:
        sections.extend(("stderr:", stderr.rstrip()))
    return "\n".join(sections)
