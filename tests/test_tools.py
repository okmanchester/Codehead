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
    if name == "bash":
        assert definition.parameters["properties"]["workdir"]["type"] == "string"


def test_write_read_and_edit_round_trip(tmp_path: Path) -> None:
    write = WriteTool(tmp_path)
    read = ReadTool(tmp_path)
    edit = EditTool(tmp_path)

    write_result = write.execute(
        {"path": "nested/example.txt", "content": "hello world"}
    )
    assert "11 characters" in write_result
    assert read.execute({"path": "nested/example.txt"}) == "1: hello world\n"

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
    assert read.execute({"path": "nested/example.txt"}) == "1: hello Codehead\n"


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
    with pytest.raises(ToolExecutionError, match="File not found"):
        ReadTool(tmp_path).execute({"path": "missing.txt"})


def test_edit_requires_one_unique_match(tmp_path: Path) -> None:
    path = tmp_path / "example.txt"
    path.write_text("same same", encoding="utf-8")
    tool = EditTool(tmp_path)

    with pytest.raises(ToolExecutionError, match="2 times"):
        tool.execute({"path": "example.txt", "old_text": "same", "new_text": "new"})
    with pytest.raises(ToolExecutionError, match="not found"):
        tool.execute({"path": "example.txt", "old_text": "missing", "new_text": "new"})


def test_edit_reports_bounded_candidate_positions_without_writing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "example.txt"
    original = "header\nsame and same\n" + "same\n" * 5
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ToolExecutionError, match="7 times") as caught:
        EditTool(tmp_path).execute(
            {"path": "example.txt", "old_text": "same", "new_text": "changed"}
        )

    output = str(caught.value)
    assert "2:1:" in output
    assert "2:10:" in output
    assert "3:1:" in output
    assert output.count("  2:") + output.count("  3:") <= 5
    assert "and 2 more matches" in output
    assert path.read_text(encoding="utf-8") == original


def test_edit_multiline_match_reports_start_line_and_column(tmp_path: Path) -> None:
    path = tmp_path / "example.txt"
    original = "前缀 foo\nbar\nother foo\nbar\n"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ToolExecutionError, match="2 times") as caught:
        EditTool(tmp_path).execute(
            {"path": "example.txt", "old_text": "foo\nbar", "new_text": "done"}
        )

    output = str(caught.value)
    assert "1:4:" in output
    assert "3:7:" in output
    assert path.read_text(encoding="utf-8") == original


def test_edit_candidate_context_stays_short_on_long_lines(tmp_path: Path) -> None:
    (tmp_path / "long.txt").write_text(
        "x" * 1000 + "target" + "y" * 1000 + "target" + "z" * 1000,
        encoding="utf-8",
    )

    with pytest.raises(ToolExecutionError, match="2 times") as caught:
        EditTool(tmp_path).execute(
            {"path": "long.txt", "old_text": "target", "new_text": "updated"}
        )

    candidates = str(caught.value).splitlines()[1:3]
    assert len(candidates) == 2
    assert all(len(candidate) <= 140 for candidate in candidates)


def test_edit_missing_match_gives_short_preview_and_recovery_hint(
    tmp_path: Path,
) -> None:
    path = tmp_path / "example.txt"
    path.write_text("actual\n", encoding="utf-8")
    old_text = "X" * 200 + "\nnot present"

    with pytest.raises(ToolExecutionError, match="not found") as caught:
        EditTool(tmp_path).execute(
            {"path": "example.txt", "old_text": old_text, "new_text": "new"}
        )

    output = str(caught.value)
    assert "old_text starts with" in output
    assert "use read or grep" in output
    assert "X" * 200 not in output
    assert path.read_text(encoding="utf-8") == "actual\n"


def test_edit_rejects_identical_text_before_writing(tmp_path: Path) -> None:
    path = tmp_path / "example.txt"
    path.write_text("same\n", encoding="utf-8")
    before = path.stat().st_mtime_ns

    with pytest.raises(ToolExecutionError, match="identical"):
        EditTool(tmp_path).execute(
            {"path": "example.txt", "old_text": "same", "new_text": "same"}
        )

    assert path.read_text(encoding="utf-8") == "same\n"
    assert path.stat().st_mtime_ns == before


def test_edit_can_delete_unique_text(tmp_path: Path) -> None:
    path = tmp_path / "example.txt"
    path.write_text("keep delete", encoding="utf-8")

    result = EditTool(tmp_path).execute(
        {"path": "example.txt", "old_text": " delete", "new_text": ""}
    )

    assert result.startswith("Edited ")
    assert path.read_text(encoding="utf-8") == "keep"


def test_bash_executes_successful_command(tmp_path: Path) -> None:
    result = BashTool(tmp_path).execute({"command": "python -c \"print('shell-ok')\""})

    assert "Exit code: 0" in result
    assert "shell-ok" in result


def test_bash_workdir_applies_to_one_call_only(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    tool = BashTool(tmp_path)
    command = 'python -c "import os; print(os.path.basename(os.getcwd()))"'

    nested_result = tool.execute({"command": command, "workdir": "nested"})
    root_result = tool.execute({"command": command})
    absolute_result = tool.execute({"command": command, "workdir": str(nested)})

    assert "stdout:\nnested" in nested_result
    assert f"stdout:\n{tmp_path.name}" in root_result
    assert "stdout:\nnested" in absolute_result


@pytest.mark.parametrize(
    ("workdir", "error"),
    [
        ("", "non-empty string"),
        (123, "non-empty string"),
        ("missing", "does not exist"),
        ("file.txt", "not a directory"),
        ("..", "outside the workspace"),
    ],
)
def test_bash_rejects_invalid_workdir_before_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workdir: object,
    error: str,
) -> None:
    (tmp_path / "file.txt").write_text("text", encoding="utf-8")
    popen = Mock()
    monkeypatch.setattr("codehead.tools.bash.subprocess.Popen", popen)

    with pytest.raises(ToolExecutionError, match=error):
        BashTool(tmp_path).execute(
            {"command": "echo should-not-run", "workdir": workdir}
        )
    popen.assert_not_called()


def test_bash_rejects_absolute_workdir_outside_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    popen = Mock()
    monkeypatch.setattr("codehead.tools.bash.subprocess.Popen", popen)

    with pytest.raises(ToolExecutionError, match="outside the workspace"):
        BashTool(tmp_path).execute(
            {"command": "echo should-not-run", "workdir": str(tmp_path.parent)}
        )
    popen.assert_not_called()


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

    assert result.startswith("1: " + "界" * 100)
    assert "[line truncated" in result
    assert "unread tail" not in result
    assert len(result.encode("utf-8")) < 50 * 1024


def test_read_exact_byte_boundary_and_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "boundary.txt"
    path.write_bytes("界".encode() * (40 * 1024 // 3) + b"a")
    result = ReadTool(tmp_path).execute({"path": "boundary.txt"})
    assert result.startswith("1: " + "界" * 100)
    assert "[line truncated" in result

    path.write_bytes(b"valid\xffinvalid")
    with pytest.raises(ToolExecutionError, match="Invalid UTF-8"):
        ReadTool(tmp_path).execute({"path": "boundary.txt"})


def test_read_pages_numbered_lines_and_reports_next_offset(tmp_path: Path) -> None:
    (tmp_path / "lines.txt").write_text("a\nb\nc\n", encoding="utf-8")
    tool = ReadTool(tmp_path)

    assert tool.execute({"path": "lines.txt", "limit": 2}) == (
        "1: a\n2: b\n"
        "[output truncated; more lines available. Continue read on the same path "
        "with next_offset=3, limit=2]"
    )
    assert tool.execute({"path": "lines.txt", "offset": 3, "limit": 2}) == "3: c\n"
    assert "end of file" in tool.execute({"path": "lines.txt", "offset": 4})


@pytest.mark.parametrize(
    "arguments",
    [
        {"offset": 0},
        {"offset": True},
        {"limit": 0},
        {"limit": 501},
        {"limit": "2"},
    ],
)
def test_read_rejects_invalid_pagination(
    tmp_path: Path, arguments: dict[str, object]
) -> None:
    (tmp_path / "file.txt").write_text("text", encoding="utf-8")
    with pytest.raises(ToolExecutionError, match="must be an integer"):
        ReadTool(tmp_path).execute({"path": "file.txt", **arguments})


def test_read_distinguishes_directory_and_binary(tmp_path: Path) -> None:
    (tmp_path / "binary.bin").write_bytes(b"hello\0world")
    tool = ReadTool(tmp_path)
    with pytest.raises(ToolExecutionError, match="Is a directory"):
        tool.execute({"path": "."})
    with pytest.raises(ToolExecutionError, match="Binary file"):
        tool.execute({"path": "binary.bin"})


def test_read_classifies_other_io_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "file.txt"
    path.write_text("text", encoding="utf-8")
    original_open = Path.open

    def denied_open(self: Path, *args, **kwargs):
        if self == path:
            raise PermissionError("denied")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_open)
    with pytest.raises(ToolExecutionError, match="Could not read.*denied"):
        ReadTool(tmp_path).execute({"path": "file.txt"})


def test_read_exact_2_kib_line_is_complete(tmp_path: Path) -> None:
    (tmp_path / "boundary.txt").write_bytes(b"a" * (2 * 1024) + b"\nnext\n")
    result = ReadTool(tmp_path).execute({"path": "boundary.txt"})
    assert "1: " + "a" * (2 * 1024) + "\n" in result
    assert "2: next\n" in result
    assert "line truncated" not in result


def test_read_output_stays_within_40_kib_and_continues(tmp_path: Path) -> None:
    (tmp_path / "many.txt").write_text(("界" * 600 + "\n") * 500, encoding="utf-8")
    result = ReadTool(tmp_path).execute({"path": "many.txt", "limit": 500})
    assert len(result.encode("utf-8")) <= 40 * 1024
    assert "next_offset=" in result


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
