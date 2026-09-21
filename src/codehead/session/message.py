"""Internal message types independent of any model SDK."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MessageRole = Literal["system", "user", "assistant", "tool"]


class ToolCall(BaseModel):
    """A tool invocation requested by the model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """The output associated with one tool call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_call_id: str
    output: str
    is_error: bool = False


class Message(BaseModel):
    """A single item in the Codehead conversation history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: MessageRole
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_result: ToolResult | None = None
