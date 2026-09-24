"""Provider-independent tool calling models."""

from copy import deepcopy
from typing import Literal

from pydantic import Field, field_validator, model_validator

from codehead.models._base import CodeheadModel

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


class ToolDefinition(CodeheadModel):
    """A tool definition exposed to a model provider."""

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    parameters: JsonObject

    @field_validator("parameters", mode="before")
    @classmethod
    def copy_parameters(cls, value: object) -> object:
        """Detach the definition snapshot from its mutable source schema."""
        return deepcopy(value)


class ToolCall(CodeheadModel):
    """A tool invocation requested by the model."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: JsonObject = Field(default_factory=dict)

    @field_validator("arguments", mode="before")
    @classmethod
    def copy_arguments(cls, value: object) -> object:
        """Detach the call snapshot from its mutable source arguments."""
        return deepcopy(value)


class ToolResult(CodeheadModel):
    """The execution result associated with one tool call."""

    tool_call_id: str = Field(min_length=1)
    output: str
    is_error: bool = False

    outcome: Literal["succeeded", "failed", "cancelled", "skipped", "denied"] = (
        "succeeded"
    )

    @model_validator(mode="before")
    @classmethod
    def execution_outcome(cls, value: object) -> object:
        if isinstance(value, dict):
            value = dict(value)
            value.setdefault(
                "outcome", "failed" if value.get("is_error") else "succeeded"
            )
            error = value["outcome"] != "succeeded"
            if "is_error" in value and value["is_error"] != error:
                raise ValueError("outcome and is_error disagree")
            value["is_error"] = error
        return value
