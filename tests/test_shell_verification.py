"""Regression cases distilled from the first three SWE-bench tool traces."""

import os

import pytest

from codehead.models import ToolCall
from codehead.tools import BashTool, ToolRegistry, ToolRuntime


@pytest.mark.skipif(os.name == "nt", reason="Unix Bash failure semantics")
@pytest.mark.parametrize(
    "command,exit_code,marker",
    [
        ("sh -c 'echo test-failed; exit 7' | tail -n 1", 7, "test-failed"),
        ("sh -c 'echo test-failed; exit 7' | grep test", 7, "test-failed"),
        ("sh -c 'echo test-failed; exit 7'; echo later-success", 7, "test-failed"),
        (
            "sh -c 'echo test-failed; exit 7' | grep test; echo later-success",
            7,
            "test-failed",
        ),
        ("printf 'ok\\n' | tail -n 1", 0, "ok"),
        ("sh -c 'exit 7' || echo recovered", 0, "recovered"),
        ("if sh -c 'exit 7'; then :; else echo expected; fi", 0, "expected"),
        ("sh -c 'exit 7' && echo skipped; echo continued", 0, "continued"),
        ("yes sample | head -n 1", 141, "sample"),
        ("echo 'ERROR: example text'; exit 0", 0, "ERROR: example text"),
    ],
)
def test_shell_verification_status(tmp_path, command, exit_code, marker):
    runtime = ToolRuntime(ToolRegistry([BashTool(tmp_path)]))
    result = runtime.execute(
        ToolCall(id="verification", name="bash", arguments={"command": command})
    )
    assert result.outcome == ("failed" if exit_code else "succeeded")
    assert result.is_error is bool(exit_code)
    assert f"Exit code: {exit_code}" in result.output
    assert marker in result.output
    if exit_code:
        assert "later-success" not in result.output
