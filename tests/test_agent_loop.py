from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar

import pytest

from codehead.agent import Agent, AgentLoop
from codehead.errors import AgentAbortedError, ModelError
from codehead.llm import LLMProvider
from codehead.models import (
    AssistantMessage,
    FinishReason,
    JsonObject,
    JsonValue,
    LLMRequest,
    LLMResponse,
    ToolCall,
    ToolMessage,
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

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        return next(self._responses)


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

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.call_count += 1
        return response(
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

    assert loop.run(session, "hello") == "first"
    assert loop.run(session, "again") == "second"
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
                    )
                ),
                FinishReason.TOOL_CALLS,
            ),
            response(AssistantMessage(content="done")),
        ]
    )
    session = Session()

    result = make_loop(provider, ToolRegistry([tool])).run(session, "record it")

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

    result = make_loop(provider, ToolRegistry([ReadTool(tmp_path)])).run(
        session,
        "read",
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

    make_loop(provider, ToolRegistry([tool])).run(Session(), "record both")

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
        ToolCall(id="rejected-call", name="record", arguments={"value": "bad"}),
    ) if has_tool else ()
    provider = FakeProvider(
        [
            response(AssistantMessage(content="previous answer")),
            response(AssistantMessage(content=content, tool_calls=calls), reason),
        ]
    )
    loop = make_loop(provider, ToolRegistry([tool]))
    session = Session()
    assert loop.run(session, "first turn") == "previous answer"
    history = session.messages

    with pytest.raises(ModelError, match=error):
        loop.run(session, "next turn")

    assert tool.values == []
    assert len(provider.calls) == 2
    assert session.messages == (*history, UserMessage(content="next turn"))


def test_default_step_limit_aborts_repeating_tool_calls() -> None:
    provider = RepeatingProvider()

    with pytest.raises(AgentAbortedError, match="20 steps"):
        make_loop(provider, ToolRegistry([RecordingTool()])).run(
            Session(), "never finish"
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
