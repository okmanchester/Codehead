"""Small real-tool smoke gate inside the installed inference runtime."""

from pathlib import Path

from codehead.models import ToolCall
from codehead.tools import BashTool, ToolRegistry, ToolRuntime


def verify_shell(workspace: Path) -> list[dict]:
    runtime = ToolRuntime(ToolRegistry([BashTool(workspace)]))
    records = []
    for command, expected in (
        ("sh -c 'echo pipeline-failure; exit 7' | tail -n 1", 7),
        ("sh -c 'echo pipeline-failure; exit 7' | grep failure; echo hidden", 7),
        ("sh -c 'echo sequence-failure; exit 7'; echo hidden", 7),
        ("sh -c 'exit 7' || echo recovered", 0),
    ):
        result = runtime.execute(
            ToolCall(id="runtime-check", name="bash", arguments={"command": command})
        )
        if (
            result.is_error is not bool(expected)
            or result.outcome != ("failed" if expected else "succeeded")
            or f"Exit code: {expected}" not in result.output
            or (expected and "hidden" in result.output)
        ):
            raise RuntimeError(
                f"Installed Shell regression failed: {command}: {result}"
            )
        records.append({"command": command, **result.model_dump(mode="json")})
    return records
