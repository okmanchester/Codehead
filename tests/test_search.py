import sys
from pathlib import Path

import pytest

from codehead.errors import ToolExecutionError
from codehead.tools import GlobTool, GrepTool
from codehead.tools.search import find_ripgrep


def test_glob_returns_relative_paths_and_clear_empty_result(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "one.py").write_text("pass", encoding="utf-8")
    (tmp_path / "nested" / "two.txt").write_text("text", encoding="utf-8")
    tool = GlobTool(tmp_path)

    assert tool.execute({"pattern": "*.py"}) == "nested/one.py\n"
    assert tool.execute({"pattern": "*.rs"}) == "No matches."
    assert tool.execute({"pattern": "*.txt", "path": "nested"}) == "nested/two.txt\n"


def test_grep_regex_glob_line_preview_and_default_ignores(tmp_path: Path) -> None:
    (tmp_path / "one.py").write_text("alpha\nBeta 123\nbeta 456\n", encoding="utf-8")
    (tmp_path / "other.txt").write_text("beta 789\n", encoding="utf-8")
    (tmp_path / ".hidden.py").write_text("beta 000\n", encoding="utf-8")
    tool = GrepTool(tmp_path)

    assert "one.py:3:beta 456\n" in tool.execute(
        {"pattern": r"beta \d+", "glob": "*.py"}
    )
    assert tool.execute({"pattern": "not-present"}) == "No matches."
    assert ".hidden.py" not in tool.execute({"pattern": "beta"})
    (tmp_path / "long.txt").write_text("MATCH" + "x" * 2000, encoding="utf-8")
    result = tool.execute({"pattern": "MATCH", "path": "long.txt"})
    assert "long.txt:1:MATCH" in result
    assert len(result.encode()) < 500


def test_grep_invalid_regex_is_error(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("text", encoding="utf-8")
    with pytest.raises(ToolExecutionError, match="ripgrep search failed"):
        GrepTool(tmp_path).execute({"pattern": "["})


def test_grep_respects_ignore_binary_and_case_rules(tmp_path: Path) -> None:
    (tmp_path / ".ignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "binary.dat").write_bytes(b"needle\0more")
    (tmp_path / "visible.txt").write_text("Needle\nneedle\n", encoding="utf-8")

    assert GrepTool(tmp_path).execute({"pattern": "needle"}) == "visible.txt:2:needle\n"


def test_grep_stops_at_40_kib_output_budget(tmp_path: Path) -> None:
    (tmp_path / "many.txt").write_text(
        ("hit" + "x" * 290 + "\n") * 200, encoding="utf-8"
    )
    result = GrepTool(tmp_path).execute({"pattern": "hit", "limit": 200})
    assert len(result.encode("utf-8")) <= 40 * 1024
    assert "results truncated" in result
    assert result.count("many.txt:") < 200


@pytest.mark.parametrize(
    "tool_type,arguments",
    [
        (GlobTool, {"pattern": "*"}),
        (GrepTool, {"pattern": "hit"}),
    ],
)
def test_search_caps_results_and_output(tmp_path: Path, tool_type, arguments) -> None:
    for number in range(230):
        (tmp_path / f"file{number:03}.txt").write_text("hit\n", encoding="utf-8")
    result = tool_type(tmp_path).execute({**arguments, "limit": 200})
    assert result.count("file") == 200
    assert "results truncated" in result
    assert len(result.encode("utf-8")) <= 40 * 1024


@pytest.mark.parametrize("arguments", [{"limit": 0}, {"limit": 201}, {"limit": True}])
def test_search_rejects_invalid_limit(tmp_path: Path, arguments) -> None:
    with pytest.raises(ToolExecutionError, match="limit"):
        GlobTool(tmp_path).execute({"pattern": "*", **arguments})


def test_search_rejects_path_outside_workspace(tmp_path: Path) -> None:
    with pytest.raises(ToolExecutionError, match="outside the workspace"):
        GrepTool(tmp_path).execute({"pattern": "x", "path": str(tmp_path.parent)})


def test_missing_ripgrep_reports_install_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("codehead.tools.search.shutil.which", lambda _: None)
    with pytest.raises(ValueError, match="ripgrep.*required"):
        find_ripgrep()


def test_search_times_out_and_cleans_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codehead.tools.process import ManagedProcess

    def slow_process(*args, **kwargs):
        return ManagedProcess(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            **kwargs,
        )

    monkeypatch.setattr("codehead.tools.search.ManagedProcess", slow_process)
    monkeypatch.setattr("codehead.tools.search.SEARCH_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(ToolExecutionError, match="timed out"):
        GlobTool(tmp_path).execute({"pattern": "*"})
