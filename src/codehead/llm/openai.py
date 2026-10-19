"""OpenAI-compatible Chat Completions provider."""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI, OpenAIError
from pydantic import ValidationError

from codehead.errors import ModelError
from codehead.llm.base import LLMProvider
from codehead.models import (
    AssistantMessage,
    FinishReason,
    LLMRequest,
    LLMResponse,
    LLMResponseCompleted,
    LLMStreamEvent,
    LLMTextDelta,
    Message,
    SystemMessage,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolMessage,
    UserMessage,
)


class OpenAIProvider(LLMProvider):
    """Asynchronous streaming adapter for the OpenAI Chat Completions protocol."""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        *,
        disable_thinking: bool = True,
        max_output_tokens: int | None = None,
        client: AsyncOpenAI | None = None,
    ) -> None:
        if max_output_tokens is not None and (
            type(max_output_tokens) is not int or max_output_tokens < 1
        ):
            raise ValueError("max_output_tokens must be a positive integer")
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.disable_thinking = disable_thinking
        self.max_output_tokens = max_output_tokens
        self._owns_client = client is None
        self._client = client or AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=0,
        )

    async def aclose(self) -> None:
        """Close an owned client; injected clients remain the caller's responsibility."""
        if self._owns_client:
            await self._client.close()

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_message_payload(message) for message in request.messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.tools:
            payload["tools"] = [
                _tool_definition_payload(definition) for definition in request.tools
            ]
        if self.max_output_tokens is not None:
            payload["max_tokens"] = self.max_output_tokens
        if self.disable_thinking:
            payload["extra_body"] = {"thinking": {"type": "disabled"}}

        text_parts: list[str] = []
        calls: dict[int, _ToolCallBuffer] = {}
        finish_reason = None
        usage = None
        saw_choice = False
        try:
            stream = await self._client.chat.completions.create(**payload)
            async with stream:
                async for chunk in stream:
                    if chunk.usage is not None:
                        usage = _parse_usage(chunk.usage)
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    saw_choice = True
                    if finish_reason is not None:
                        raise ModelError(
                            "Model returned content after its finish reason"
                        )
                    delta = choice.delta
                    if delta.content is not None:
                        if not isinstance(delta.content, str):
                            raise ModelError("Model returned an invalid response")
                        text_parts.append(delta.content)
                        if delta.content:
                            yield LLMTextDelta(delta=delta.content)
                    for fragment in delta.tool_calls or ():
                        index = fragment.index
                        if not isinstance(index, int) or index < 0:
                            raise ModelError(
                                "Model returned an invalid tool call index"
                            )
                        if fragment.type not in (None, "function"):
                            raise ModelError("Model returned an unsupported tool call")
                        call = calls.setdefault(index, _ToolCallBuffer())
                        call.id += fragment.id or ""
                        if fragment.function is not None:
                            call.name += fragment.function.name or ""
                            call.arguments += fragment.function.arguments or ""
                    finish_reason = choice.finish_reason
            if not saw_choice:
                raise ModelError("Model stream did not contain any choices")
            response = LLMResponse(
                message=AssistantMessage(
                    content="".join(text_parts) if text_parts else None,
                    tool_calls=tuple(calls[index].parse() for index in sorted(calls)),
                ),
                finish_reason=_finish_reason(finish_reason),
                usage=usage,
            )
        except OpenAIError as exc:
            raise ModelError(f"Model request failed: {exc}") from exc
        except ModelError:
            raise
        except (AttributeError, TypeError, ValueError, ValidationError) as exc:
            raise ModelError("Model returned an invalid response") from exc
        yield LLMResponseCompleted(response=response)


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


@dataclass
class _ToolCallBuffer:
    id: str = ""
    name: str = ""
    arguments: str = ""

    def parse(self) -> ToolCall:
        try:
            arguments = json.loads(self.arguments)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ModelError("Model returned an invalid function tool call") from exc
        if not isinstance(arguments, dict):
            raise ModelError("Tool call arguments must be a JSON object")
        try:
            return ToolCall(id=self.id, name=self.name, arguments=arguments)
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
