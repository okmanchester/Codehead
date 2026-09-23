import asyncio
import threading
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import ClassVar

import pytest

from codehead.agent import Agent, AgentLoop
from codehead.errors import AgentAbortedError, ModelError
from codehead.llm import LLMProvider
from codehead.models import (
    AssistantMessage,
    AssistantTextDelta,
    FinishReason,
    JsonObject,
    JsonValue,
    LLMRequest,
    LLMResponse,
    LLMResponseCompleted,
    LLMStreamEvent,
    LLMTextDelta,
    ModelCompleted,
    ModelStarted,
    ToolCall,
    ToolCompleted,
    ToolMessage,
    ToolStarted,
    TurnCompleted,
    UserMessage,
)
from codehead.prompt import build_system_prompt
from codehead.session import Session
from codehead.tools import ReadTool, Tool, ToolRegistry


def response(
    message: AssistantMessage,
    finish_reason: FinishReason = FinishReason.END_TURN,
) -> LLMResponse:
    return LLMResponse(message=message, finish_reason=finish_reason)


class FakeProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = iter(responses)
        self.calls: list[LLMRequest] = []

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        self.calls.append(request)
        result = next(self._responses)
        if result.message.content:
            yield LLMTextDelta(delta=result.message.content)
        yield LLMResponseCompleted(response=result)


class RecordingTool(Tool):
    name = "record"
    description = "Record and return a value."
    parameters: ClassVar[JsonObject] = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    def __init__(self) -> None:
        self.values: list[str] = []

    def execute(self, arguments: Mapping[str, JsonValue]) -> str:
        value = str(arguments["value"])
        self.values.append(value)
        return value


class RepeatingProvider(LLMProvider):
    def __init__(self) -> None:
        self.call_count = 0

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        self.call_count += 1
        yield LLMResponseCompleted(
            response=response(
                AssistantMessage(
                    tool_calls=(
                        ToolCall(
                            id=f"call-{self.call_count}",
                            name="record",
                            arguments={"value": "again"},
                        ),
                    )
                ),
                FinishReason.TOOL_CALLS,
            )
        )


def make_loop(
    provider: LLMProvider,
    tools: ToolRegistry | None = None,
    *,
    max_steps: int = 20,
) -> AgentLoop:
    agent = Agent(
        provider=provider,
        tools=tools or ToolRegistry(),
        system_prompt="system prompt",
    )
    return AgentLoop(agent, max_steps=max_steps)


def test_direct_final_answer_and_system_prompt_added_once() -> None:
    provider = FakeProvider(
        [
            response(AssistantMessage(content="first")),
            response(AssistantMessage(content="second")),
        ]
    )
    loop = make_loop(provider)
    session = Session()

    assert asyncio.run(loop.run(session, "hello")) == "first"
    assert asyncio.run(loop.run(session, "again")) == "second"
    assert [message.role for message in session.messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert provider.calls[0].messages == session.messages[:2]
    assert provider.calls[1].messages == session.messages[:-1]


@pytest.mark.parametrize("content", [None, "I will record the value."])
def test_tool_call_result_round_trip(content: str | None) -> None:
    tool = RecordingTool()
    provider = FakeProvider(
        [
            response(
                AssistantMessage(
                    content=content,
                    tool_calls=(
                        ToolCall(
                            id="call-1",
                            name="record",
                            arguments={"value": "captured"},
                        ),
                    ),
                ),
                FinishReason.TOOL_CALLS,
            ),
            response(AssistantMessage(content="done")),
        ]
    )
    session = Session()

    result = asyncio.run(
        make_loop(provider, ToolRegistry([tool])).run(session, "record it")
    )

    assert result == "done"
    assert tool.values == ["captured"]
    tool_message = session.messages[3]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.content == "captured"
    assert tool_message.is_error is False
    assert provider.calls[1].messages[-1] == tool_message
    assert provider.calls[1].messages[-2].content == content


def test_tool_error_is_returned_to_model(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            response(
                AssistantMessage(
                    tool_calls=(
                        ToolCall(
                            id="call-1",
                            name="read",
                            arguments={"path": "README.md"},
                        ),
                    )
                ),
                FinishReason.TOOL_CALLS,
            ),
            response(AssistantMessage(content="handled")),
        ]
    )
    session = Session()

    result = asyncio.run(
        make_loop(provider, ToolRegistry([ReadTool(tmp_path)])).run(
            session,
            "read",
        )
    )

    assert result == "handled"
    result_seen_by_model = provider.calls[1].messages[-1]
    assert isinstance(result_seen_by_model, ToolMessage)
    assert result_seen_by_model.is_error is True
    assert "Failed to read" in result_seen_by_model.content


def test_multiple_tool_calls_execute_in_order() -> None:
    tool = RecordingTool()
    provider = FakeProvider(
        [
            response(
                AssistantMessage(
                    tool_calls=(
                        ToolCall(
                            id="call-1",
                            name="record",
                            arguments={"value": "one"},
                        ),
                        ToolCall(
                            id="call-2",
                            name="record",
                            arguments={"value": "two"},
                        ),
                    )
                ),
                FinishReason.TOOL_CALLS,
            ),
            response(AssistantMessage(content="done")),
        ]
    )

    asyncio.run(make_loop(provider, ToolRegistry([tool])).run(Session(), "record both"))

    assert tool.values == ["one", "two"]


@pytest.mark.parametrize(
    ("reason", "content", "has_tool", "error"),
    [
        (FinishReason.END_TURN, None, False, "non-blank text"),
        (FinishReason.END_TURN, "", False, "non-blank text"),
        (FinishReason.END_TURN, " \n\t", False, "non-blank text"),
        (FinishReason.END_TURN, None, True, "must not contain tool calls"),
        (FinishReason.END_TURN, "done", True, "must not contain tool calls"),
        (FinishReason.TOOL_CALLS, None, False, "must contain tool calls"),
        (FinishReason.TOOL_CALLS, "done", False, "must contain tool calls"),
        *[
            (reason, content, has_tool, error)
            for reason, error in (
                (FinishReason.MAX_TOKENS, "truncated"),
                (FinishReason.CONTENT_FILTER, "filtered"),
                (FinishReason.UNKNOWN, "unknown finish reason"),
            )
            for content, has_tool in (
                (None, False),
                ("partial answer", False),
                (None, True),
                ("partial answer", True),
            )
        ],
    ],
)
def test_rejected_response_has_no_side_effects(
    reason: FinishReason,
    content: str | None,
    has_tool: bool,
    error: str,
) -> None:
    tool = RecordingTool()
    calls = (
        (ToolCall(id="rejected-call", name="record", arguments={"value": "bad"}),)
        if has_tool
        else ()
    )
    provider = FakeProvider(
        [
            response(AssistantMessage(content="previous answer")),
            response(AssistantMessage(content=content, tool_calls=calls), reason),
        ]
    )
    loop = make_loop(provider, ToolRegistry([tool]))
    session = Session()
    assert asyncio.run(loop.run(session, "first turn")) == "previous answer"
    history = session.messages

    with pytest.raises(ModelError, match=error):
        asyncio.run(loop.run(session, "next turn"))

    assert tool.values == []
    assert len(provider.calls) == 2
    assert session.messages == (*history, UserMessage(content="next turn"))


def test_default_step_limit_aborts_repeating_tool_calls() -> None:
    provider = RepeatingProvider()

    with pytest.raises(AgentAbortedError, match="20 steps"):
        asyncio.run(
            make_loop(provider, ToolRegistry([RecordingTool()])).run(
                Session(), "never finish"
            )
        )

    assert provider.call_count == 20


def test_system_prompt_includes_only_root_agents_file(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("Root instructions", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "AGENTS.md").write_text("Nested instructions", encoding="utf-8")

    prompt = build_system_prompt(tmp_path)

    assert str(tmp_path.resolve()) in prompt
    assert "Root instructions" in prompt
    assert "Nested instructions" not in prompt


def test_event_order_and_session_commit_boundaries() -> None:
    async def check():
        tool = RecordingTool()
        calls = tuple(
            ToolCall(id=f"call-{value}", name="record", arguments={"value": value})
            for value in ("one", "two")
        )
        provider = FakeProvider(
            [
                response(
                    AssistantMessage(content="working", tool_calls=calls),
                    FinishReason.TOOL_CALLS,
                ),
                response(AssistantMessage(content="done")),
            ]
        )
        session = Session()
        events = []
        async for event in make_loop(provider, ToolRegistry([tool])).stream(
            session, "go"
        ):
            events.append(event)
            if isinstance(event, AssistantTextDelta):
                assert session.messages[-1].role in ("user", "tool")
            elif isinstance(event, ModelCompleted):
                assert session.messages[-1] == event.response.message
            elif isinstance(event, ToolStarted):
                assert event.tool_call.arguments["value"] not in tool.values
            elif isinstance(event, ToolCompleted):
                assert session.messages[-1].tool_call_id == event.result.tool_call_id
                assert session.messages[-1].content == event.result.output
            elif isinstance(event, TurnCompleted):
                assert event.answer == "done"
        assert [type(event) for event in events] == [
            ModelStarted,
            AssistantTextDelta,
            ModelCompleted,
            ToolStarted,
            ToolCompleted,
            ToolStarted,
            ToolCompleted,
            ModelStarted,
            AssistantTextDelta,
            ModelCompleted,
            TurnCompleted,
        ]
        assert [event.step for event in events] == [1] * 7 + [2] * 4
        assert tool.values == ["one", "two"]

    asyncio.run(check())


def test_failed_tool_emits_result_and_model_can_finish() -> None:
    async def check():
        provider = FakeProvider(
            [
                response(
                    AssistantMessage(
                        tool_calls=(ToolCall(id="bad", name="unknown", arguments={}),)
                    ),
                    FinishReason.TOOL_CALLS,
                ),
                response(AssistantMessage(content="handled")),
            ]
        )
        events = [event async for event in make_loop(provider).stream(Session(), "go")]
        result = next(
            event.result for event in events if isinstance(event, ToolCompleted)
        )
        assert result.is_error
        assert "Unknown tool" in result.output
        assert provider.calls[1].messages[-1].is_error
        assert isinstance(events[-1], TurnCompleted)

    asyncio.run(check())


@pytest.mark.parametrize(
    "failure", [ModelError("interrupted"), asyncio.CancelledError()]
)
def test_partial_response_failure_preserves_history_and_closes_provider(
    failure,
) -> None:
    class InterruptedProvider(LLMProvider):
        closed = False

        async def stream(self, request):
            try:
                yield LLMTextDelta(delta="partial")
                raise failure
            finally:
                self.closed = True

    async def check():
        provider = InterruptedProvider()
        session = Session(
            [UserMessage(content="old"), AssistantMessage(content="old answer")]
        )
        history = session.messages
        events = []
        with pytest.raises(type(failure)):
            async for event in make_loop(provider).stream(session, "new"):
                events.append(event)
        assert [type(event) for event in events] == [ModelStarted, AssistantTextDelta]
        assert session.messages == (*history, UserMessage(content="new"))
        assert provider.closed

    asyncio.run(check())


def test_closing_loop_stream_closes_provider_without_committing_delta() -> None:
    class PartialProvider(LLMProvider):
        closed = False

        async def stream(self, request):
            try:
                yield LLMTextDelta(delta="partial")
                raise AssertionError("must not resume provider")
            finally:
                self.closed = True

    async def check():
        provider = PartialProvider()
        session = Session()
        events = make_loop(provider).stream(session, "go")
        assert isinstance(await anext(events), ModelStarted)
        assert isinstance(await anext(events), AssistantTextDelta)
        await events.aclose()
        assert provider.closed
        assert [message.role for message in session.messages] == ["system", "user"]

    asyncio.run(check())


@pytest.mark.parametrize("extra", ["missing", "duplicate", "late_delta"])
def test_malformed_provider_event_sequence_is_rejected_before_commit(extra) -> None:
    class MalformedProvider(LLMProvider):
        async def stream(self, request):
            yield LLMTextDelta(delta="partial")
            if extra != "missing":
                event = LLMResponseCompleted(
                    response=response(AssistantMessage(content="done"))
                )
                yield event
                yield event if extra == "duplicate" else LLMTextDelta(delta="late")

    async def check():
        provider = MalformedProvider()
        session = Session()
        with pytest.raises(ModelError):
            await make_loop(provider).run(session, "go")
        assert [message.role for message in session.messages] == ["system", "user"]
        with pytest.raises(ModelError):
            await provider.complete(LLMRequest(messages=()))

    asyncio.run(check())


def test_sync_tool_does_not_block_event_loop() -> None:
    async def check():
        event_loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()

        class WaitingTool(RecordingTool):
            def execute(self, arguments):
                event_loop.call_soon_threadsafe(entered.set)
                if not release.wait(2):
                    raise RuntimeError("event loop did not release tool")
                return super().execute(arguments)

        provider = FakeProvider(
            [
                response(
                    AssistantMessage(
                        tool_calls=(
                            ToolCall(
                                id="wait", name="record", arguments={"value": "ok"}
                            ),
                        )
                    ),
                    FinishReason.TOOL_CALLS,
                ),
                response(AssistantMessage(content="done")),
            ]
        )
        tool = WaitingTool()
        task = asyncio.create_task(
            make_loop(provider, ToolRegistry([tool])).run(Session(), "go")
        )
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert not task.done()
            assert tool.values == []
            release.set()
            assert await asyncio.wait_for(task, 1) == "done"
            assert tool.values == ["ok"]
            assert not provider.calls[1].messages[-1].is_error
        finally:
            release.set()
            await task

    asyncio.run(check())


def test_cancelling_waiting_model_closes_stream() -> None:
    async def check():
        entered = asyncio.Event()
        closed = asyncio.Event()

        class WaitingProvider(LLMProvider):
            async def stream(self, request):
                try:
                    yield LLMTextDelta(delta="partial")
                    entered.set()
                    await asyncio.Event().wait()
                finally:
                    closed.set()

        session = Session()
        task = asyncio.create_task(make_loop(WaitingProvider()).run(session, "go"))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        assert [message.role for message in session.messages] == ["system", "user"]

    asyncio.run(check())
