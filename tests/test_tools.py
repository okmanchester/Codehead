import pytest

from codehead.errors import ToolExecutionError
from codehead.tools import BashTool, EditTool, ReadTool, Tool, WriteTool


@pytest.mark.parametrize(
    ("tool", "name", "required"),
    [
        (ReadTool(), "read", ["path"]),
        (WriteTool(), "write", ["path", "content"]),
        (EditTool(), "edit", ["path", "old_text", "new_text"]),
        (BashTool(), "bash", ["command"]),
    ],
)
def test_placeholder_tool_schema_and_execution_error(
    tool: Tool,
    name: str,
    required: list[str],
) -> None:
    schema = tool.schema()

    assert schema["name"] == name
    assert schema["description"]
    assert schema["parameters"]["type"] == "object"
    assert schema["parameters"]["required"] == required
    assert schema["parameters"]["additionalProperties"] is False

    with pytest.raises(ToolExecutionError, match="not implemented"):
        tool.execute({})
