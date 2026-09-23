import platform
from pathlib import Path
from unittest.mock import Mock

import pytest

from codehead.errors import ToolExecutionError
from codehead.models import ToolDefinition
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
def test_tool_definition(
    tmp_path: Path,
    tool_type: type[Tool],
    name: str,
    required: list[str],
) -> None:
    tool = tool_type(tmp_path)
    definition = tool.definition()

    assert isinstance(definition, ToolDefinition)
    assert definition.name == name
    assert definition.description
    assert definition.parameters["type"] == "object"
    assert definition.parameters["required"] == required
    assert definition.parameters["additionalProperties"] is False


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
        tool.execute({"path": "example.txt", "old_text": "same", "new_text": "new"})
    with pytest.raises(ToolExecutionError, match="not found"):
        tool.execute({"path": "example.txt", "old_text": "missing", "new_text": "new"})


def test_bash_executes_successful_command(tmp_path: Path) -> None:
    result = BashTool(tmp_path).execute({"command": "python -c \"print('shell-ok')\""})

    assert "Exit code: 0" in result
    assert "shell-ok" in result


def test_bash_reports_nonzero_exit(tmp_path: Path) -> None:
    with pytest.raises(ToolExecutionError, match="Exit code: 3"):
        BashTool(tmp_path).execute(
            {"command": ("python -c \"import sys; print('bad'); sys.exit(3)\"")}
        )


def test_bash_reports_timeout(
    tmp_path: Path,
) -> None:
    with pytest.raises(ToolExecutionError, match="timed out after 2 seconds") as caught:
        BashTool(tmp_path, timeout_seconds=2).execute(
            {
                "command": "python -c \"import time; print('started', flush=True); time.sleep(5)\""
            }
        )
    assert "started" in str(caught.value)


def test_read_limits_source_bytes_and_marks_truncation(tmp_path: Path) -> None:
    content = "界" * 15000 + "unread tail"
    (tmp_path / "large.txt").write_text(content, encoding="utf-8")

    result = ReadTool(tmp_path).execute({"path": "large.txt"})

    assert result.startswith("界" * 100)
    assert "[output truncated]" in result
    assert "unread tail" not in result
    assert len(result.encode("utf-8")) < 50 * 1024


def test_read_exact_byte_boundary_and_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "boundary.txt"
    path.write_bytes("界".encode() * (40 * 1024 // 3) + b"a")
    result = ReadTool(tmp_path).execute({"path": "boundary.txt"})
    assert result == path.read_text(encoding="utf-8")
    assert "truncated" not in result

    path.write_bytes(b"valid\xffinvalid")
    with pytest.raises(ToolExecutionError, match="Failed to read"):
        ReadTool(tmp_path).execute({"path": "boundary.txt"})


@pytest.mark.parametrize("exit_code", [0, 3])
def test_bash_drains_large_stdout_and_stderr_with_head_tail(
    tmp_path: Path, exit_code: int
) -> None:
    command = (
        'python -c "import sys; '
        "sys.stdout.write('OUT_HEAD'+'x'*1000000+'OUT_TAIL'); "
        "sys.stderr.write('ERR_HEAD'+'y'*1000000+'ERR_TAIL'); "
        f'sys.exit({exit_code})"'
    )
    tool = BashTool(tmp_path, timeout_seconds=15)
    if exit_code:
        with pytest.raises(ToolExecutionError) as caught:
            tool.execute({"command": command})
        output = str(caught.value)
    else:
        output = tool.execute({"command": command})

    assert f"Exit code: {exit_code}" in output
    assert all(
        text in output for text in ("OUT_HEAD", "OUT_TAIL", "ERR_HEAD", "ERR_TAIL")
    )
    assert output.count("[output truncated]") == 2
    assert len(output.encode("utf-8")) < 50 * 1024


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
    popen = Mock()
    process = popen.return_value
    process.stdout.read.side_effect = [b"ok\n", b""]
    process.stderr.read.return_value = b""
    process.wait.return_value = 0
    process.poll.return_value = 0
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    monkeypatch.setattr("codehead.tools.bash.platform.system", lambda: system_name)
    monkeypatch.setattr("codehead.tools.bash.subprocess.Popen", popen)

    BashTool(tmp_path).execute({"command": "echo ok"})

    assert popen.call_args.args[0][0] == executable
    assert "DEEPSEEK_API_KEY" not in popen.call_args.kwargs["env"]
