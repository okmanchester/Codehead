"""OpenAI-compatible Chat Completions provider."""

import json
from typing import Any

from openai import OpenAI, OpenAIError
from pydantic import ValidationError

from codehead.errors import ModelError
from codehead.llm.base import LLMProvider
from codehead.models import (
    AssistantMessage,
    FinishReason,
    LLMRequest,
    LLMResponse,
    Message,
    SystemMessage,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolMessage,
    UserMessage,
)


class OpenAIProvider(LLMProvider):
    """Synchronous adapter for the OpenAI Chat Completions protocol."""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        *,
        disable_thinking: bool = True,
        client: OpenAI | None = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.disable_thinking = disable_thinking
        self._client = client or OpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=0,
        )

    def complete(self, request: LLMRequest) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                _message_payload(message) for message in request.messages
            ],
            "stream": False,
        }
        if request.tools:
            payload["tools"] = [
                _tool_definition_payload(definition)
                for definition in request.tools
            ]
        if self.disable_thinking:
            payload["extra_body"] = {"thinking": {"type": "disabled"}}

        try:
            response = self._client.chat.completions.create(**payload)
        except OpenAIError as exc:
            raise ModelError(f"Model request failed: {exc}") from exc

        try:
            return _parse_response(response)
        except ModelError:
            raise
        except (AttributeError, TypeError, ValueError, ValidationError) as exc:
            raise ModelError("Model returned an invalid response") from exc


def _message_payload(message: Message) -> dict[str, Any]:
    if isinstance(message, SystemMessage):
        return {"role": "system", "content": message.content}

    if isinstance(message, UserMessage):
        return {"role": "user", "content": message.content}

    if isinstance(message, AssistantMessage):
        payload: dict[str, Any] = {
            "role": "assistant",
            "content": message.content,
        }
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.name,
                        "arguments": json.dumps(
                            tool_call.arguments,
                            ensure_ascii=False,
                        ),
                    },
                }
                for tool_call in message.tool_calls
            ]
        return payload

    if isinstance(message, ToolMessage):
        content = message.content
        if message.is_error:
            content = f"Error: {content}"
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": content,
        }

    raise ModelError(f"Unsupported message type: {type(message).__name__}")


def _tool_definition_payload(definition: ToolDefinition) -> dict[str, Any]:
    return {
        "type": "function",
        "function": definition.model_dump(mode="json"),
    }


def _parse_response(response: Any) -> LLMResponse:
    if not response.choices:
        raise ModelError("Model response did not contain any choices")

    choice = response.choices[0]
    response_message = choice.message
    tool_calls = tuple(
        _parse_tool_call(tool_call)
        for tool_call in (response_message.tool_calls or ())
    )
    return LLMResponse(
        message=AssistantMessage(
            content=response_message.content,
            tool_calls=tool_calls,
        ),
        finish_reason=_finish_reason(getattr(choice, "finish_reason", None)),
        usage=_parse_usage(getattr(response, "usage", None)),
    )


def _parse_tool_call(tool_call: Any) -> ToolCall:
    try:
        name = tool_call.function.name
        raw_arguments = tool_call.function.arguments
        arguments = json.loads(raw_arguments)
        call_id = tool_call.id
    except (AttributeError, json.JSONDecodeError, TypeError) as exc:
        raise ModelError("Model returned an invalid function tool call") from exc

    if not isinstance(arguments, dict):
        raise ModelError("Tool call arguments must be a JSON object")

    try:
        return ToolCall(id=call_id, name=name, arguments=arguments)
    except ValidationError as exc:
        raise ModelError("Model returned an invalid function tool call") from exc


def _parse_usage(usage: Any) -> TokenUsage | None:
    if usage is None:
        return None
    return TokenUsage(
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
    )


def _finish_reason(value: object) -> FinishReason:
    if not isinstance(value, str):
        return FinishReason.UNKNOWN
    reasons = {
        "stop": FinishReason.END_TURN,
        "tool_calls": FinishReason.TOOL_CALLS,
        "function_call": FinishReason.TOOL_CALLS,
        "length": FinishReason.MAX_TOKENS,
        "content_filter": FinishReason.CONTENT_FILTER,
    }
    return reasons.get(value, FinishReason.UNKNOWN)
