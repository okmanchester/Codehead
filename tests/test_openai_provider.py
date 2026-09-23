import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx2
import pytest
from openai import AsyncOpenAI, OpenAIError

from codehead.errors import ModelError
from codehead.llm import OpenAIProvider
from codehead.models import (
    AssistantMessage,
    FinishReason,
    LLMRequest,
    LLMResponse,
    LLMResponseCompleted,
    LLMTextDelta,
    SystemMessage,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolMessage,
    UserMessage,
)


class FakeStream:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        chunk = next(self.chunks, None)
        if chunk is None:
            raise StopAsyncIteration
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk


def make_client(response: object) -> Mock:
    client = Mock()
    client.stream = FakeStream(response if isinstance(response, list) else [response])
    client.chat.completions.create = AsyncMock(return_value=client.stream)
    client.close = AsyncMock()
    return client


def chat_response(
    *,
    content: object = None,
    tool_calls: list[object] | None = None,
    finish_reason: object = "stop",
    usage: object = None,
) -> object:
    for index, call in enumerate(tool_calls or ()):
        call.index = index
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason, index=0)
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

    result = asyncio.run(provider.complete(request))

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
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
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

    result = asyncio.run(provider.complete(LLMRequest(messages=())))

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
        asyncio.run(provider.complete(LLMRequest(messages=())))


def test_provider_rejects_invalid_response_message() -> None:
    provider = OpenAIProvider(
        "deepseek-flash",
        "secret",
        client=make_client(chat_response(content=[{"type": "text"}])),
    )

    with pytest.raises(ModelError, match="invalid response"):
        asyncio.run(provider.complete(LLMRequest(messages=())))


def test_provider_rejects_empty_choices() -> None:
    provider = OpenAIProvider(
        "deepseek-flash",
        "secret",
        client=make_client(SimpleNamespace(choices=[], usage=None)),
    )

    with pytest.raises(ModelError, match="choices"):
        asyncio.run(provider.complete(LLMRequest(messages=())))


def test_provider_wraps_sdk_errors() -> None:
    client = make_client(chat_response(content="unused"))
    client.chat.completions.create.side_effect = OpenAIError("unavailable")
    provider = OpenAIProvider("deepseek-flash", "secret", client=client)

    with pytest.raises(ModelError, match="unavailable"):
        asyncio.run(provider.complete(LLMRequest(messages=())))


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

    result = asyncio.run(provider.complete(LLMRequest(messages=())))

    assert result.finish_reason is expected


def test_provider_disables_sdk_retries() -> None:
    with patch("codehead.llm.openai.AsyncOpenAI") as client_type:
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


def test_stream_yields_text_before_waiting_for_completion_and_usage() -> None:
    async def check():
        release = asyncio.Event()
        client = make_client([])

        class GatedStream(FakeStream):
            async def __anext__(self):
                if self.first:
                    self.first = False
                    return chat_response(content="hello", finish_reason=None)
                await release.wait()
                return await super().__anext__()

        stream = GatedStream(
            [
                chat_response(content=" world"),
                SimpleNamespace(choices=[], usage=usage()),
            ]
        )
        stream.first = True
        client.chat.completions.create.return_value = stream
        provider = OpenAIProvider("model", "secret", client=client)
        events = provider.stream(LLMRequest(messages=()))
        try:
            assert await asyncio.wait_for(anext(events), 1) == LLMTextDelta(
                delta="hello"
            )
            pending = asyncio.create_task(anext(events))
            await asyncio.sleep(0)
            assert not pending.done()
            release.set()
            assert await pending == LLMTextDelta(delta=" world")
            completed = await anext(events)
            assert isinstance(completed, LLMResponseCompleted)
            assert completed.response.message.content == "hello world"
            assert completed.response.usage.total_tokens == 15
            assert stream.closed
            with pytest.raises(StopAsyncIteration):
                await anext(events)
        finally:
            release.set()
            await events.aclose()

    asyncio.run(check())


def test_stream_assembles_interleaved_tool_fragments_in_index_order() -> None:
    def chunk(index, call_id, name, arguments):
        result = chat_response(
            tool_calls=[function_call(call_id, name, arguments)],
            finish_reason=None,
        )
        result.choices[0].delta.tool_calls[0].index = index
        return result

    client = make_client(
        [
            chat_response(content="", finish_reason=None),
            chunk(1, "call-", "ba", '{"command":'),
            chunk(0, "call-0", "re", '{"path": "'),
            chunk(1, "1", "sh", '"pwd"}'),
            chunk(0, None, "ad", 'a.txt"}'),
            chat_response(finish_reason="tool_calls"),
        ]
    )
    provider = OpenAIProvider("model", "secret", client=client)
    result = asyncio.run(provider.complete(LLMRequest(messages=())))
    assert result.message.tool_calls == (
        ToolCall(id="call-0", name="read", arguments={"path": "a.txt"}),
        ToolCall(id="call-1", name="bash", arguments={"command": "pwd"}),
    )
    assert result.message.content == ""
    assert result.usage is None
    assert client.stream.closed


@pytest.mark.parametrize(
    "failure", [OpenAIError("lost connection"), asyncio.CancelledError()]
)
def test_stream_failure_after_partial_text_closes_without_completion(failure) -> None:
    async def check():
        client = make_client(
            [
                chat_response(content="partial", finish_reason=None),
                failure,
            ]
        )
        provider = OpenAIProvider("model", "secret", client=client)
        events = provider.stream(LLMRequest(messages=()))
        assert await anext(events) == LLMTextDelta(delta="partial")
        expected = (
            asyncio.CancelledError
            if isinstance(failure, asyncio.CancelledError)
            else ModelError
        )
        with pytest.raises(expected):
            await anext(events)
        assert client.stream.closed

    asyncio.run(check())


def test_early_close_does_not_consume_remaining_chunks() -> None:
    async def check():
        client = make_client(
            [
                chat_response(content="partial", finish_reason=None),
                AssertionError("must not consume the next chunk"),
            ]
        )
        provider = OpenAIProvider("model", "secret", client=client)
        events = provider.stream(LLMRequest(messages=()))
        await anext(events)
        await events.aclose()
        assert client.stream.closed
        await provider.aclose()
        client.close.assert_not_awaited()

    asyncio.run(check())


def test_provider_closes_owned_client() -> None:
    client = make_client([])
    with patch("codehead.llm.openai.AsyncOpenAI", return_value=client):
        provider = OpenAIProvider("model", "secret")
        asyncio.run(provider.aclose())
    client.close.assert_awaited_once()


@pytest.mark.parametrize(
    "chunks",
    [
        [],
        [SimpleNamespace(choices=[], usage=usage())],
        [chat_response(tool_calls=[function_call("", "read", "{}")])],
        [chat_response(tool_calls=[function_call("id", "", "{}")])],
        [chat_response(tool_calls=[function_call("id", "read", "{")])],
        [chat_response(content="done"), chat_response(content="extra")],
    ],
)
def test_invalid_stream_closes_without_a_completed_event(chunks) -> None:
    async def check():
        client = make_client(chunks)
        provider = OpenAIProvider("model", "secret", client=client)
        seen = []
        with pytest.raises(ModelError):
            async for event in provider.stream(LLMRequest(messages=())):
                seen.append(event)
        assert not any(isinstance(event, LLMResponseCompleted) for event in seen)
        assert client.stream.closed

    asyncio.run(check())


@pytest.mark.parametrize("disconnect", [False, True])
def test_real_sdk_decodes_sse_and_closes_http_stream(disconnect) -> None:
    def sse(choices, usage=None):
        data = {
            "id": "completion",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "model",
            "choices": choices,
            "usage": usage,
        }
        return ("data: " + json.dumps(data) + "\n\n").encode()

    class Body(httpx2.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield sse(
                [{"index": 0, "delta": {"content": "hello"}, "finish_reason": None}]
            )
            if disconnect:
                raise httpx2.ReadError("connection lost")
            yield sse([{"index": 0, "delta": {}, "finish_reason": "stop"}])
            yield sse(
                [], {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
            )
            yield b"data: [DONE]\n\n"

        async def aclose(self):
            self.closed = True

    async def check():
        body = Body()

        def handle(request):
            assert json.loads(request.content)["stream"] is True
            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, stream=body
            )

        async with AsyncOpenAI(
            api_key="test",
            base_url="https://example.test",
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)),
        ) as client:
            provider = OpenAIProvider("model", "test", client=client)
            events = provider.stream(LLMRequest(messages=()))
            assert await anext(events) == LLMTextDelta(delta="hello")
            if disconnect:
                with pytest.raises(ModelError, match="Model request failed"):
                    await anext(events)
            else:
                completed = await anext(events)
                assert completed.response.usage.total_tokens == 3
                assert completed.response.finish_reason is FinishReason.END_TURN
            await events.aclose()
            assert body.closed

    asyncio.run(check())
