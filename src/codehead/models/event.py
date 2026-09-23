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


class TurnEvent(CodeheadModel):
    turn_id: str | None = None


class TurnStarted(TurnEvent):
    type: Literal["turn.started"] = "turn.started"


class TurnFailed(TurnEvent):
    type: Literal["turn.failed"] = "turn.failed"
    reason: str


class TurnCancelled(TurnEvent):
    type: Literal["turn.cancelled"] = "turn.cancelled"


class ModelStarted(TurnEvent):
    type: Literal["model.started"] = "model.started"
    step: int = Field(ge=1)


class ContextCompactionStarted(TurnEvent):
    type: Literal["context.compaction_started"] = "context.compaction_started"
    step: int = Field(ge=1)
    before_tokens: int = Field(ge=0)


class ContextCompacted(TurnEvent):
    type: Literal["context.compacted"] = "context.compacted"
    step: int = Field(ge=1)
    before_tokens: int = Field(ge=0)
    after_tokens: int = Field(ge=0)


class AssistantTextDelta(TurnEvent):
    type: Literal["assistant.text_delta"] = "assistant.text_delta"
    step: int = Field(ge=1)
    delta: str = Field(min_length=1)


class ModelCompleted(TurnEvent):
    type: Literal["model.completed"] = "model.completed"
    step: int = Field(ge=1)
    response: LLMResponse


class ToolStarted(TurnEvent):
    execution_id: str | None = None
    type: Literal["tool.started"] = "tool.started"
    step: int = Field(ge=1)
    tool_call: ToolCall


class ToolCompleted(TurnEvent):
    execution_id: str | None = None
    type: Literal["tool.completed"] = "tool.completed"
    step: int = Field(ge=1)
    result: ToolResult


class TurnCompleted(TurnEvent):
    type: Literal["turn.completed"] = "turn.completed"
    step: int = Field(ge=1)
    answer: str


AgentEvent = Annotated[
    TurnStarted
    | TurnFailed
    | TurnCancelled
    | ModelStarted
    | ContextCompactionStarted
    | ContextCompacted
    | AssistantTextDelta
    | ModelCompleted
    | ToolStarted
    | ToolCompleted
    | TurnCompleted,
    Field(discriminator="type"),
]
