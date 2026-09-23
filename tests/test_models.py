import pytest
from pydantic import ValidationError

from codehead.models import (
    AssistantMessage,
    FinishReason,
    LLMRequest,
    LLMResponse,
    SystemMessage,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolMessage,
    ToolResult,
    UserMessage,
)


def test_tool_result_outcome_matches_legacy_error_flag():
    assert ToolResult(tool_call_id="call", output="ok").outcome == "succeeded"
    assert (
        ToolResult(tool_call_id="call", output="error", is_error=True).outcome
        == "failed"
    )
    cancelled = ToolResult(tool_call_id="call", output="stopped", outcome="cancelled")
    assert cancelled.is_error
    with pytest.raises(ValidationError, match="outcome and is_error disagree"):
        ToolResult(tool_call_id="call", output="bad", outcome="skipped", is_error=False)


def test_request_parses_messages_as_role_discriminated_union() -> None:
    request = LLMRequest.model_validate(
        {
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "hello"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "name": "read",
                            "arguments": {"path": "a.txt"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "content": "contents",
                },
            ]
        }
    )

    assert isinstance(request.messages[0], SystemMessage)
    assert isinstance(request.messages[1], UserMessage)
    assert isinstance(request.messages[2], AssistantMessage)
    assert isinstance(request.messages[3], ToolMessage)
    assert isinstance(request.messages, tuple)
    assert isinstance(request.messages[2].tool_calls, tuple)


def test_role_specific_messages_reject_invalid_fields_and_roles() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        LLMRequest.model_validate(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "hello",
                        "tool_calls": [],
                    }
                ]
            }
        )

    with pytest.raises(ValidationError, match="union_tag_invalid"):
        LLMRequest.model_validate(
            {"messages": [{"role": "developer", "content": "hello"}]}
        )


def test_empty_assistant_message_is_a_valid_domain_value() -> None:
    response = LLMResponse(
        message=AssistantMessage(),
        finish_reason=FinishReason.UNKNOWN,
    )

    assert response.message.content is None
    assert response.message.tool_calls == ()


def test_domain_models_are_structurally_immutable() -> None:
    request = LLMRequest(messages=(UserMessage(content="hello"),))

    with pytest.raises(ValidationError, match="frozen_instance"):
        request.messages = ()


def test_tool_json_inputs_are_defensively_copied() -> None:
    arguments = {"nested": {"items": ["one"]}}
    parameters = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
    }
    call = ToolCall(id="call-1", name="echo", arguments=arguments)
    definition = ToolDefinition(
        name="echo",
        description="Echo a value.",
        parameters=parameters,
    )

    arguments["nested"]["items"].append("two")
    parameters["properties"]["other"] = {"type": "number"}

    assert call.arguments == {"nested": {"items": ["one"]}}
    assert definition.parameters == {
        "type": "object",
        "properties": {"value": {"type": "string"}},
    }


def test_tool_models_reject_non_json_values_and_empty_identifiers() -> None:
    with pytest.raises(ValidationError):
        ToolCall(id="call-1", name="echo", arguments={"bad": object()})
    with pytest.raises(ValidationError, match="string_too_short"):
        ToolResult(tool_call_id="", output="result")


def test_token_usage_rejects_negative_counts() -> None:
    with pytest.raises(ValidationError, match="greater_than_equal"):
        TokenUsage(input_tokens=-1, output_tokens=0, total_tokens=0)
