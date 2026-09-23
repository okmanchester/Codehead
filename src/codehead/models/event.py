"""Typed progress events; deltas are provisional until a response is accepted."""

from typing import Annotated, Literal

from pydantic import Field

from codehead.models._base import CodeheadModel
from codehead.models.llm import LLMResponse
from codehead.models.tool import ToolCall, ToolResult


class LLMTextDelta(CodeheadModel):
    type: Literal["llm.text_delta"] = "llm.text_delta"
    delta: str = Field(min_length=1)


class LLMResponseCompleted(CodeheadModel):
    type: Literal["llm.response_completed"] = "llm.response_completed"
    response: LLMResponse


LLMStreamEvent = Annotated[
    LLMTextDelta | LLMResponseCompleted, Field(discriminator="type")
]


class ModelStarted(CodeheadModel):
    type: Literal["model.started"] = "model.started"
    step: int = Field(ge=1)


class AssistantTextDelta(CodeheadModel):
    type: Literal["assistant.text_delta"] = "assistant.text_delta"
    step: int = Field(ge=1)
    delta: str = Field(min_length=1)


class ModelCompleted(CodeheadModel):
    type: Literal["model.completed"] = "model.completed"
    step: int = Field(ge=1)
    response: LLMResponse


class ToolStarted(CodeheadModel):
    type: Literal["tool.started"] = "tool.started"
    step: int = Field(ge=1)
    tool_call: ToolCall


class ToolCompleted(CodeheadModel):
    type: Literal["tool.completed"] = "tool.completed"
    step: int = Field(ge=1)
    result: ToolResult


class TurnCompleted(CodeheadModel):
    type: Literal["turn.completed"] = "turn.completed"
    step: int = Field(ge=1)
    answer: str


AgentEvent = Annotated[
    ModelStarted
    | AssistantTextDelta
    | ModelCompleted
    | ToolStarted
    | ToolCompleted
    | TurnCompleted,
    Field(discriminator="type"),
]
