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


def thinking_tools():
    return (ToolDefinition(name="read", description="read", parameters={}),)


@pytest.mark.parametrize("thinking", ["enabled", "disabled"])
@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_thinking_configuration_and_request_override(thinking, effort):
    client = make_client([])
    client.chat.completions.create.side_effect = [
        FakeStream([chat_response(content="answer", reasoning_content="thought")]),
        FakeStream([chat_response(content="summary")]),
    ]
    provider = OpenAIProvider(
        "test", "secret", thinking=thinking, reasoning_effort=effort, client=client
    )
    asyncio.run(provider.complete(LLMRequest(messages=())))
    asyncio.run(provider.complete(LLMRequest(messages=(), thinking="disabled")))
    first, second = [
        call.kwargs for call in client.chat.completions.create.call_args_list
    ]
    assert first["extra_body"] == {"thinking": {"type": thinking}}
    assert first.get("reasoning_effort") == (effort if thinking == "enabled" else None)
    assert second["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "reasoning_effort" not in second
    assert provider.thinking == thinking


@pytest.mark.parametrize(
    "field,value",
    [("thinking", "auto"), ("thinking", True), ("reasoning_effort", "medium")],
)
def test_invalid_thinking_constructor(field, value):
    with pytest.raises(ValueError, match=field):
        OpenAIProvider("test", "secret", **{field: value})


@pytest.mark.parametrize("reasoning", [None, "", "thought"])
@pytest.mark.parametrize("has_tools", [False, True])
def test_existing_reasoning_is_forwarded_even_when_thinking_disabled(
    reasoning, has_tools
):
    client = make_client(chat_response(content="done"))
    provider = OpenAIProvider("test", "secret", client=client)
    asyncio.run(
        provider.complete(
            LLMRequest(
                messages=(
                    AssistantMessage(content="old", reasoning_content=reasoning),
                ),
                tools=thinking_tools() if has_tools else (),
            )
        )
    )
    message = client.chat.completions.create.call_args.kwargs["messages"][0]
    assert ("reasoning_content" in message) == (has_tools and reasoning is not None)
    if "reasoning_content" in message:
        assert message["reasoning_content"] == reasoning


def test_missing_history_reasoning_is_rejected_before_network():
    client = make_client(chat_response(content="done"))
    provider = OpenAIProvider("test", "secret", thinking="enabled", client=client)
    with pytest.raises(ModelError, match="start a new session or disable thinking"):
        asyncio.run(
            provider.complete(
                LLMRequest(
                    messages=(AssistantMessage(content="old"),), tools=thinking_tools()
                )
            )
        )
    client.chat.completions.create.assert_not_called()


def test_reasoning_stream_is_accumulated_separately_from_text():
    client = make_client(
        [
            chat_response(reasoning_content="", finish_reason=None),
            chat_response(reasoning_content="think ", finish_reason=None),
            chat_response(reasoning_content="more", content="answer"),
        ]
    )
    provider = OpenAIProvider("test", "secret", thinking="enabled", client=client)

    async def run():
        return [
            event
            async for event in provider.stream(
                LLMRequest(messages=(), tools=thinking_tools())
            )
        ]

    events = asyncio.run(run())
    assert events[0] == LLMTextDelta(delta="answer")
    assert len(events) == 2
    assert events[-1].response.message.reasoning_content == "think more"


@pytest.mark.parametrize("reasoning", [None, "", 7, False, []])
def test_required_response_reasoning_distinguishes_missing_empty_and_invalid(reasoning):
    client = make_client(chat_response(content="done", reasoning_content=reasoning))
    provider = OpenAIProvider("test", "secret", thinking="enabled", client=client)
    request = LLMRequest(messages=(), tools=thinking_tools())
    if reasoning == "":
        assert asyncio.run(provider.complete(request)).message.reasoning_content == ""
    else:
        with pytest.raises(ModelError, match="reasoning_content"):
            asyncio.run(provider.complete(request))


@pytest.mark.parametrize(
    "finish,expected",
    [
        ("length", FinishReason.MAX_TOKENS),
        ("content_filter", FinishReason.CONTENT_FILTER),
        ("unexpected", FinishReason.UNKNOWN),
    ],
)
def test_missing_reasoning_does_not_mask_error_finish_reason(finish, expected):
    provider = OpenAIProvider(
        "test",
        "secret",
        thinking="enabled",
        client=make_client(chat_response(content="partial", finish_reason=finish)),
    )
    result = asyncio.run(
        provider.complete(LLMRequest(messages=(), tools=thinking_tools()))
    )
    assert result.finish_reason == expected


def test_usage_completion_total_already_includes_reasoning_tokens():
    total = usage(prompt_tokens=10, completion_tokens=100, total_tokens=110)
    total.completion_tokens_details = SimpleNamespace(reasoning_tokens=80)
    provider = OpenAIProvider(
        "test",
        "secret",
        thinking="enabled",
        client=make_client(
            chat_response(content="answer", reasoning_content="thought", usage=total)
        ),
    )
    result = asyncio.run(provider.complete(LLMRequest(messages=())))
    assert result.usage == TokenUsage(
        input_tokens=10, output_tokens=100, total_tokens=110
    )


def test_thinking_tool_loop_preserves_reasoning_across_calls_and_user_turns(tmp_path):
    from codehead.agent import Agent, AgentLoop
    from codehead.permissions import PermissionPolicy
    from codehead.session import Session
    from codehead.tools import ReadTool, ToolRegistry

    (tmp_path / "data.txt").write_text("data", "utf-8")
    tools = ToolRegistry([ReadTool(tmp_path)])
    client = make_client([])
    client.chat.completions.create.side_effect = [
        FakeStream(
            [
                chat_response(
                    reasoning_content=f"thought {i}",
                    tool_calls=[
                        function_call(f"read-{i}", "read", '{"path":"data.txt"}')
                    ],
                    finish_reason="tool_calls",
                )
            ]
        )
        for i in range(2)
    ] + [
        FakeStream(
            [chat_response(content="first done", reasoning_content="final thought")]
        ),
        FakeStream(
            [chat_response(content="next done", reasoning_content="next thought")]
        ),
    ]
    provider = OpenAIProvider("test", "secret", thinking="enabled", client=client)
    loop = AgentLoop(
        Agent(provider=provider, tools=tools, system_prompt="sys"),
        permission_policy=PermissionPolicy.allow_all(tmp_path, ["read"]),
    )

    async def run():
        session = Session()
        try:
            assert await loop.run(session, "first") == "first done"
            assert await loop.run(session, "second") == "next done"
            assert [
                m.reasoning_content
                for m in session.messages
                if isinstance(m, AssistantMessage)
            ] == ["thought 0", "thought 1", "final thought", "next thought"]
        finally:
            session.close()
            await loop.aclose()

    asyncio.run(run())
    payloads = [call.kwargs for call in client.chat.completions.create.call_args_list]
    assert [
        m["reasoning_content"]
        for m in payloads[-1]["messages"]
        if m["role"] == "assistant"
    ] == [
        "thought 0",
        "thought 1",
        "final thought",
    ]


@pytest.mark.parametrize("failure", ["missing", "truncated", "cancelled"])
def test_failed_thinking_stream_never_commits_assistant_or_executes_tools(
    tmp_path, failure
):
    from codehead.agent import Agent, AgentLoop
    from codehead.permissions import PermissionPolicy
    from codehead.session import Session
    from codehead.tools import ReadTool, ToolRegistry

    tool = ReadTool(tmp_path)
    chunk = chat_response(
        tool_calls=[function_call("one", "read", '{"path":"missing"}')],
        finish_reason="length" if failure == "truncated" else "tool_calls",
    )
    if failure == "cancelled":
        chunk.choices[0].finish_reason = None
        chunks = [chunk, asyncio.CancelledError()]
    else:
        chunks = [chunk]
    provider = OpenAIProvider(
        "test", "secret", thinking="enabled", client=make_client(chunks)
    )
    loop = AgentLoop(
        Agent(provider=provider, tools=ToolRegistry([tool]), system_prompt="sys"),
        permission_policy=PermissionPolicy.allow_all(tmp_path, ["read"]),
    )

    async def run():
        session = Session()
        try:
            with patch.object(tool, "execute") as execute:
                with pytest.raises(
                    asyncio.CancelledError if failure == "cancelled" else ModelError
                ):
                    await loop.run(session, "read")
                execute.assert_not_called()
            assert not any(isinstance(m, AssistantMessage) for m in session.messages)
        finally:
            session.close()
            await loop.aclose()

    asyncio.run(run())


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


@pytest.mark.parametrize("limit", [8192, 1])
def test_provider_optional_output_limit(limit):
    client = make_client(chat_response(content="done"))
    provider = OpenAIProvider("test", "secret", max_output_tokens=limit, client=client)
    asyncio.run(provider.complete(LLMRequest(messages=(UserMessage(content="hello"),))))
    assert client.chat.completions.create.call_args.kwargs["max_tokens"] == limit


@pytest.mark.parametrize("limit", [0, -1, True, "8192"])
def test_provider_rejects_invalid_output_limit(limit):
    with pytest.raises(ValueError, match="max_output_tokens"):
        OpenAIProvider("test", "secret", max_output_tokens=limit)


def make_client(response: object) -> Mock:
    client = Mock()
    client.stream = FakeStream(response if isinstance(response, list) else [response])
    client.chat.completions.create = AsyncMock(return_value=client.stream)
    client.close = AsyncMock()
    return client


def chat_response(
    *,
    content: object = None,
    reasoning_content: object = None,
    tool_calls: list[object] | None = None,
    finish_reason: object = "stop",
    usage: object = None,
) -> object:
    for index, call in enumerate(tool_calls or ()):
        call.index = index
    delta = SimpleNamespace(
        content=content, reasoning_content=reasoning_content, tool_calls=tool_calls
    )
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
    assert "max_tokens" not in payload
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
