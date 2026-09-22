from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

import pytest

from codehead.agent import Agent, AgentLoop
from codehead.errors import AgentAbortedError, ModelError
from codehead.llm import LLMProvider
from codehead.prompt import build_system_prompt
from codehead.session import Message, Session, ToolCall
from codehead.tools import ReadTool, Tool, ToolRegistry, ToolSchema


class FakeProvider(LLMProvider):
    def __init__(self, responses: list[Message]) -> None:
        self._responses = iter(responses)
        self.calls: list[tuple[tuple[Message, ...], tuple[ToolSchema, ...]]] = []

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema],
    ) -> Message:
        self.calls.append((tuple(messages), tuple(tools)))
        return next(self._responses)


class RecordingTool(Tool):
    name = "record"
    description = "Record and return a value."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    def __init__(self) -> None:
        self.values: list[str] = []

    def execute(self, arguments: Mapping[str, Any]) -> str:
        value = str(arguments["value"])
        self.values.append(value)
        return value


class RepeatingProvider(LLMProvider):
    def __init__(self) -> None:
        self.call_count = 0

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema],
    ) -> Message:
        self.call_count += 1
        return Message(
            role="assistant",
            tool_calls=[
                ToolCall(
                    id=f"call-{self.call_count}",
                    name="record",
                    arguments={"value": "again"},
                )
            ],
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
            Message(role="assistant", content="first"),
            Message(role="assistant", content="second"),
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


def test_tool_call_result_round_trip() -> None:
    tool = RecordingTool()
    provider = FakeProvider(
        [
            Message(
                role="assistant",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="record",
                        arguments={"value": "captured"},
                    )
                ],
            ),
            Message(role="assistant", content="done"),
        ]
    )
    session = Session()

    result = make_loop(provider, ToolRegistry([tool])).run(session, "record it")

    assert result == "done"
    assert tool.values == ["captured"]
    tool_message = session.messages[3]
    assert tool_message.role == "tool"
    assert tool_message.tool_result is not None
    assert tool_message.tool_result.output == "captured"
    assert tool_message.tool_result.is_error is False


def test_tool_error_is_returned_to_model(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            Message(
                role="assistant",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="read",
                        arguments={"path": "README.md"},
                    )
                ],
            ),
            Message(role="assistant", content="handled"),
        ]
    )
    session = Session()

    result = make_loop(provider, ToolRegistry([ReadTool(tmp_path)])).run(
        session,
        "read",
    )

    assert result == "handled"
    result_seen_by_model = provider.calls[1][0][-1].tool_result
    assert result_seen_by_model is not None
    assert result_seen_by_model.is_error is True
    assert "Failed to read" in result_seen_by_model.output


def test_multiple_tool_calls_execute_in_order() -> None:
    tool = RecordingTool()
    provider = FakeProvider(
        [
            Message(
                role="assistant",
                tool_calls=[
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
                ],
            ),
            Message(role="assistant", content="done"),
        ]
    )

    make_loop(provider, ToolRegistry([tool])).run(Session(), "record both")

    assert tool.values == ["one", "two"]


def test_empty_model_response_raises_model_error() -> None:
    provider = FakeProvider([Message(role="assistant")])

    with pytest.raises(ModelError, match="neither text nor tool calls"):
        make_loop(provider).run(Session(), "hello")


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
