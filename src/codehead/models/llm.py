"""Models exchanged across the LLM provider boundary."""

from enum import StrEnum
from typing import Literal

from pydantic import Field

from codehead.models._base import CodeheadModel
from codehead.models.message import AssistantMessage, Message
from codehead.models.tool import ToolDefinition


class FinishReason(StrEnum):
    """Provider-independent reason that a model turn ended."""

    END_TURN = "end_turn"
    TOOL_CALLS = "tool_calls"
    MAX_TOKENS = "max_tokens"
    CONTENT_FILTER = "content_filter"
    UNKNOWN = "unknown"


class TokenUsage(CodeheadModel):
    """Minimal token counts reported by a model provider."""

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class LLMRequest(CodeheadModel):
    """Immutable snapshot of one provider request."""

    messages: tuple[Message, ...]
    tools: tuple[ToolDefinition, ...] = ()
    thinking: Literal["enabled", "disabled"] | None = None


class LLMResponse(CodeheadModel):
    """Normalized result of one provider request."""

    message: AssistantMessage
    finish_reason: FinishReason
    usage: TokenUsage | None = None
