import platform
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codehead.errors import ToolExecutionError
from codehead.tools import BashTool, EditTool, ReadTool, Tool, WriteTool


@pytest.mark.parametrize(
    ("tool_type", "name", "required"),
    [
        (ReadTool, "read", ["path"]),
        (WriteTool, "write", ["path", "content"]),
        (EditTool, "edit", ["path", "old_text", "new_text"]),
        (BashTool, "bash", ["command"]),
    ],
)
def test_tool_schema(
    tmp_path: Path,
    tool_type: type[Tool],
    name: str,
    required: list[str],
) -> None:
    tool = tool_type(tmp_path)
    schema = tool.schema()

    assert schema["name"] == name
    assert schema["description"]
    assert schema["parameters"]["type"] == "object"
    assert schema["parameters"]["required"] == required
    assert schema["parameters"]["additionalProperties"] is False


def test_write_read_and_edit_round_trip(tmp_path: Path) -> None:
    write = WriteTool(tmp_path)
    read = ReadTool(tmp_path)
    edit = EditTool(tmp_path)

    write_result = write.execute(
        {"path": "nested/example.txt", "content": "hello world"}
    )
    assert "11 characters" in write_result
    assert read.execute({"path": "nested/example.txt"}) == "hello world"

    edit_result = edit.execute(
        {
            "path": "nested/example.txt",
            "old_text": "world",
            "new_text": "Codehead",
        }
    )
    expected_path = (
        "nested\\example.txt"
        if platform.system() == "Windows"
        else "nested/example.txt"
    )
    assert edit_result == f"Edited {expected_path}"
    assert read.execute({"path": "nested/example.txt"}) == "hello Codehead"


@pytest.mark.parametrize("tool_type", [ReadTool, WriteTool, EditTool])
def test_file_tools_reject_paths_outside_workspace(
    tmp_path: Path,
    tool_type: type[Tool],
) -> None:
    outside = tmp_path.parent / "outside.txt"
    tool = tool_type(tmp_path)
    arguments = {
        "path": str(outside),
        "content": "content",
        "old_text": "old",
        "new_text": "new",
    }

    with pytest.raises(ToolExecutionError, match="outside the workspace"):
        tool.execute(arguments)


def test_read_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ToolExecutionError, match="Failed to read"):
        ReadTool(tmp_path).execute({"path": "missing.txt"})


def test_edit_requires_one_unique_match(tmp_path: Path) -> None:
    path = tmp_path / "example.txt"
    path.write_text("same same", encoding="utf-8")
    tool = EditTool(tmp_path)

    with pytest.raises(ToolExecutionError, match="2 times"):
        tool.execute(
            {"path": "example.txt", "old_text": "same", "new_text": "new"}
        )
    with pytest.raises(ToolExecutionError, match="not found"):
        tool.execute(
            {"path": "example.txt", "old_text": "missing", "new_text": "new"}
        )


def test_bash_executes_successful_command(tmp_path: Path) -> None:
    result = BashTool(tmp_path).execute(
        {"command": 'python -c "print(\'shell-ok\')"'}
    )

    assert "Exit code: 0" in result
    assert "shell-ok" in result


def test_bash_reports_nonzero_exit(tmp_path: Path) -> None:
    with pytest.raises(ToolExecutionError, match="Exit code: 3"):
        BashTool(tmp_path).execute(
            {
                "command": (
                    'python -c "import sys; print(\'bad\'); sys.exit(3)"'
                )
            }
        )


def test_bash_reports_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Mock(side_effect=subprocess.TimeoutExpired("cmd", 2))
    monkeypatch.setattr("codehead.tools.bash.subprocess.run", run)

    with pytest.raises(ToolExecutionError, match="timed out after 2 seconds"):
        BashTool(tmp_path, timeout_seconds=2).execute({"command": "slow"})


@pytest.mark.parametrize(
    ("system_name", "executable"),
    [("Windows", "powershell.exe"), ("Linux", "/bin/bash")],
)
def test_bash_selects_platform_shell_and_removes_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    system_name: str,
    executable: str,
) -> None:
    run = Mock(
        return_value=SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    monkeypatch.setattr("codehead.tools.bash.platform.system", lambda: system_name)
    monkeypatch.setattr("codehead.tools.bash.subprocess.run", run)

    BashTool(tmp_path).execute({"command": "echo ok"})

    assert run.call_args.args[0][0] == executable
    assert "DEEPSEEK_API_KEY" not in run.call_args.kwargs["env"]
