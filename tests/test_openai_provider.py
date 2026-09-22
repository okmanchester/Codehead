from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from openai import OpenAIError

from codehead.errors import ModelError
from codehead.llm import OpenAIProvider
from codehead.session import Message, ToolCall, ToolResult


def make_client(response: object) -> Mock:
    client = Mock()
    client.chat.completions.create.return_value = response
    return client


def response_message(
    *,
    content: str | None = None,
    tool_calls: list[object] | None = None,
) -> object:
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def function_call(call_id: str, name: str, arguments: str) -> object:
    function = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(id=call_id, type="function", function=function)


def test_provider_builds_chat_completion_request() -> None:
    client = make_client(response_message(content="done"))
    provider = OpenAIProvider(
        model="deepseek-flash",
        api_key="secret",
        base_url="https://example.test",
        client=client,
    )
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="hello"),
        Message(
            role="assistant",
            tool_calls=[
                ToolCall(id="call-1", name="read", arguments={"path": "a.txt"})
            ],
        ),
        Message(
            role="tool",
            tool_result=ToolResult(
                tool_call_id="call-1",
                output="missing",
                is_error=True,
            ),
        ),
    ]
    tools = [
        {
            "name": "read",
            "description": "Read a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        }
    ]

    result = provider.complete(messages, tools)

    assert result == Message(role="assistant", content="done")
    request = client.chat.completions.create.call_args.kwargs
    assert request["model"] == "deepseek-flash"
    assert request["stream"] is False
    assert request["extra_body"] == {"thinking": {"type": "disabled"}}
    assert request["tools"] == [{"type": "function", "function": tools[0]}]
    assert request["messages"] == [
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


def test_provider_parses_multiple_function_calls() -> None:
    client = make_client(
        response_message(
            tool_calls=[
                function_call("call-1", "read", '{"path": "a.txt"}'),
                function_call("call-2", "bash", '{"command": "pwd"}'),
            ]
        )
    )
    provider = OpenAIProvider("deepseek-flash", "secret", client=client)

    result = provider.complete([], [])

    assert result.tool_calls == [
        ToolCall(id="call-1", name="read", arguments={"path": "a.txt"}),
        ToolCall(id="call-2", name="bash", arguments={"command": "pwd"}),
    ]
    assert "tools" not in client.chat.completions.create.call_args.kwargs


@pytest.mark.parametrize("arguments", ["not json", "[]"])
def test_provider_rejects_invalid_tool_arguments(arguments: str) -> None:
    client = make_client(
        response_message(tool_calls=[function_call("call-1", "read", arguments)])
    )
    provider = OpenAIProvider("deepseek-flash", "secret", client=client)

    with pytest.raises(ModelError, match="tool call|JSON object"):
        provider.complete([], [])


def test_provider_rejects_empty_choices() -> None:
    provider = OpenAIProvider(
        "deepseek-flash",
        "secret",
        client=make_client(SimpleNamespace(choices=[])),
    )

    with pytest.raises(ModelError, match="choices"):
        provider.complete([], [])


def test_provider_wraps_sdk_errors() -> None:
    client = make_client(response_message(content="unused"))
    client.chat.completions.create.side_effect = OpenAIError("unavailable")
    provider = OpenAIProvider("deepseek-flash", "secret", client=client)

    with pytest.raises(ModelError, match="unavailable"):
        provider.complete([], [])


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
