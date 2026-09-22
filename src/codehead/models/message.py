"""Role-specific conversation message models."""

from typing import Annotated, Literal

from pydantic import Field

from codehead.models._base import CodeheadModel
from codehead.models.tool import ToolCall


class SystemMessage(CodeheadModel):
    """Instructions supplied by the harness."""

    role: Literal["system"] = "system"
    content: str


class UserMessage(CodeheadModel):
    """Text supplied by the user."""

    role: Literal["user"] = "user"
    content: str


class AssistantMessage(CodeheadModel):
    """Text and tool calls produced by the model."""

    role: Literal["assistant"] = "assistant"
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()


class ToolMessage(CodeheadModel):
    """A tool result recorded in the conversation."""

    role: Literal["tool"] = "tool"
    tool_call_id: str = Field(min_length=1)
    content: str
    is_error: bool = False


type Message = Annotated[
    SystemMessage | UserMessage | AssistantMessage | ToolMessage,
    Field(discriminator="role"),
]
