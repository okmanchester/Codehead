from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from openai import OpenAIError

from codehead.errors import ModelError
from codehead.llm import OpenAIProvider
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
    UserMessage,
)


def make_client(response: object) -> Mock:
    client = Mock()
    client.chat.completions.create.return_value = response
    return client


def chat_response(
    *,
    content: object = None,
    tool_calls: list[object] | None = None,
    finish_reason: object = "stop",
    usage: object = None,
) -> object:
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage)


def function_call(call_id: str, name: str, arguments: str) -> object:
    function = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(id=call_id, type="function", function=function)


def usage(
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    total_tokens: int = 15,
) -> object:
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )


def test_provider_builds_chat_completion_request() -> None:
    client = make_client(chat_response(content="done", usage=usage()))
    provider = OpenAIProvider(
        model="deepseek-flash",
        api_key="secret",
        base_url="https://example.test",
        client=client,
    )
    request = LLMRequest(
        messages=(
            SystemMessage(content="system"),
            UserMessage(content="hello"),
            AssistantMessage(
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name="read",
                        arguments={"path": "a.txt"},
                    ),
                )
            ),
            ToolMessage(
                tool_call_id="call-1",
                content="missing",
                is_error=True,
            ),
        ),
        tools=(
            ToolDefinition(
                name="read",
                description="Read a file.",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            ),
        ),
    )

    result = provider.complete(request)

    assert result == LLMResponse(
        message=AssistantMessage(content="done"),
        finish_reason=FinishReason.END_TURN,
        usage=TokenUsage(
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
        ),
    )
    payload = client.chat.completions.create.call_args.kwargs
    assert payload["model"] == "deepseek-flash"
    assert payload["stream"] is False
    assert payload["extra_body"] == {"thinking": {"type": "disabled"}}
    assert payload["tools"] == [
        {
            "type": "function",
            "function": request.tools[0].model_dump(mode="json"),
        }
    ]
    assert payload["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "read",
                        "arguments": '{"path": "a.txt"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "Error: missing"},
    ]


def test_provider_parses_multiple_function_calls_without_usage() -> None:
    client = make_client(
        chat_response(
            tool_calls=[
                function_call("call-1", "read", '{"path": "a.txt"}'),
                function_call("call-2", "bash", '{"command": "pwd"}'),
            ],
            finish_reason="tool_calls",
        )
    )
    provider = OpenAIProvider("deepseek-flash", "secret", client=client)

    result = provider.complete(LLMRequest(messages=()))

    assert result == LLMResponse(
        message=AssistantMessage(
            tool_calls=(
                ToolCall(id="call-1", name="read", arguments={"path": "a.txt"}),
                ToolCall(id="call-2", name="bash", arguments={"command": "pwd"}),
            )
        ),
        finish_reason=FinishReason.TOOL_CALLS,
    )
    assert "tools" not in client.chat.completions.create.call_args.kwargs


@pytest.mark.parametrize("arguments", ["not json", "[]"])
def test_provider_rejects_invalid_tool_arguments(arguments: str) -> None:
    client = make_client(
        chat_response(tool_calls=[function_call("call-1", "read", arguments)])
    )
    provider = OpenAIProvider("deepseek-flash", "secret", client=client)

    with pytest.raises(ModelError, match="tool call|JSON object"):
        provider.complete(LLMRequest(messages=()))


def test_provider_rejects_invalid_response_message() -> None:
    provider = OpenAIProvider(
        "deepseek-flash",
        "secret",
        client=make_client(chat_response(content=[{"type": "text"}])),
    )

    with pytest.raises(ModelError, match="invalid response"):
        provider.complete(LLMRequest(messages=()))


def test_provider_rejects_empty_choices() -> None:
    provider = OpenAIProvider(
        "deepseek-flash",
        "secret",
        client=make_client(SimpleNamespace(choices=[], usage=None)),
    )

    with pytest.raises(ModelError, match="choices"):
        provider.complete(LLMRequest(messages=()))


def test_provider_wraps_sdk_errors() -> None:
    client = make_client(chat_response(content="unused"))
    client.chat.completions.create.side_effect = OpenAIError("unavailable")
    provider = OpenAIProvider("deepseek-flash", "secret", client=client)

    with pytest.raises(ModelError, match="unavailable"):
        provider.complete(LLMRequest(messages=()))


@pytest.mark.parametrize(
    ("provider_reason", "expected"),
    [
        ("stop", FinishReason.END_TURN),
        ("tool_calls", FinishReason.TOOL_CALLS),
        ("function_call", FinishReason.TOOL_CALLS),
        ("length", FinishReason.MAX_TOKENS),
        ("content_filter", FinishReason.CONTENT_FILTER),
        ("future_reason", FinishReason.UNKNOWN),
        (None, FinishReason.UNKNOWN),
    ],
)
def test_provider_normalizes_finish_reason(
    provider_reason: str | None,
    expected: FinishReason,
) -> None:
    provider = OpenAIProvider(
        "deepseek-flash",
        "secret",
        client=make_client(
            chat_response(content="done", finish_reason=provider_reason)
        ),
    )

    result = provider.complete(LLMRequest(messages=()))

    assert result.finish_reason is expected


def test_provider_disables_sdk_retries() -> None:
    with patch("codehead.llm.openai.OpenAI") as client_type:
        OpenAIProvider(
            model="deepseek-flash",
            api_key="secret",
            base_url="https://api.deepseek.com",
        )

    client_type.assert_called_once_with(
        api_key="secret",
        base_url="https://api.deepseek.com",
        max_retries=0,
    )
