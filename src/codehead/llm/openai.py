"""OpenAI-compatible Chat Completions provider."""

import json
from collections.abc import Sequence
from typing import Any

from openai import OpenAI, OpenAIError

from codehead.errors import ModelError
from codehead.llm.base import LLMProvider
from codehead.session import Message, ToolCall
from codehead.tools.base import ToolSchema


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

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema],
    ) -> Message:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": [_message_payload(message) for message in messages],
            "stream": False,
        }
        if tools:
            request["tools"] = [
                {"type": "function", "function": schema} for schema in tools
            ]
        if self.disable_thinking:
            request["extra_body"] = {"thinking": {"type": "disabled"}}

        try:
            response = self._client.chat.completions.create(**request)
        except OpenAIError as exc:
            raise ModelError(f"Model request failed: {exc}") from exc

        if not response.choices:
            raise ModelError("Model response did not contain any choices")

        response_message = response.choices[0].message
        tool_calls = [
            _parse_tool_call(tool_call)
            for tool_call in (response_message.tool_calls or [])
        ]
        return Message(
            role="assistant",
            content=response_message.content,
            tool_calls=tool_calls,
        )


def _message_payload(message: Message) -> dict[str, Any]:
    if message.role in {"system", "user"}:
        if message.content is None:
            raise ModelError(f"{message.role} message must contain text")
        return {"role": message.role, "content": message.content}

    if message.role == "assistant":
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

    if message.tool_result is None:
        raise ModelError("tool message must contain a tool result")
    output = message.tool_result.output
    if message.tool_result.is_error:
        output = f"Error: {output}"
    return {
        "role": "tool",
        "tool_call_id": message.tool_result.tool_call_id,
        "content": output,
    }


def _parse_tool_call(tool_call: Any) -> ToolCall:
    try:
        name = tool_call.function.name
        raw_arguments = tool_call.function.arguments
        arguments = json.loads(raw_arguments)
    except (AttributeError, json.JSONDecodeError, TypeError) as exc:
        raise ModelError("Model returned an invalid function tool call") from exc

    if not isinstance(arguments, dict):
        raise ModelError("Tool call arguments must be a JSON object")

    return ToolCall(
        id=tool_call.id,
        name=name,
        arguments=arguments,
    )
