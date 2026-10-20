import asyncio
import json
from pathlib import Path
from typing import ClassVar

import pytest

from codehead.agent import Agent, AgentLoop
from codehead.agent.context import (
    ContextManager,
    base_tokens,
    estimate_tokens,
    observe_usage,
)
from codehead.errors import ContextError, ModelError, StorageError
from codehead.llm import LLMProvider
from codehead.models import (
    AssistantMessage,
    AssistantTextDelta,
    ContextCompacted,
    ContextCompactionStarted,
    FinishReason,
    LLMRequest,
    LLMResponse,
    LLMResponseCompleted,
    LLMTextDelta,
    ModelCompleted,
    ModelStarted,
    SystemMessage,
    TokenUsage,
    ToolCall,
    ToolCompleted,
    ToolDefinition,
    ToolMessage,
    ToolStarted,
    TurnStarted,
    UserMessage,
)
from codehead.permissions import ApprovalChoice, PermissionPolicy
from codehead.session import Session, SQLiteSessionStore
from codehead.tools import ReadTool, Tool, ToolRegistry


async def approve_once(call: ToolCall) -> ApprovalChoice:
    return "once"


class SummaryProvider(LLMProvider):
    def __init__(self, result=None, failure=None):
        self.calls = []
        self.result = result
        self.failure = failure
        self.closed = 0

    async def stream(self, request):
        self.calls.append(request)
        try:
            yield LLMTextDelta(delta="private summary draft")
            if self.failure:
                raise self.failure
            yield LLMResponseCompleted(
                response=self.result
                or LLMResponse(
                    message=AssistantMessage(content=f"summary {len(self.calls)}"),
                    finish_reason=FinishReason.END_TURN,
                )
            )
        finally:
            self.closed += 1


@pytest.fixture
def store(tmp_path):
    archive = SQLiteSessionStore(tmp_path / "history.sqlite3")
    yield archive
    archive.close()


def long_session(store):
    session = Session([SystemMessage(content="original instructions")], store=store)
    for index in range(5):
        session.add(UserMessage(content=f"old question {index}: " + "x" * 2000))
        session.add(AssistantMessage(content=f"old answer {index}: " + "y" * 2000))
    session.add(UserMessage(content="latest user request"))
    return session


async def prepare(manager, session, tools=()):
    return [event async for event in manager.prepare(session, tools, 1)]


def checkpoint_count(store):
    return store._connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]


def test_compaction_preserves_retained_reasoning_and_excludes_summary_input(
    store, tmp_path
):
    session = Session(
        [SystemMessage(content="sys")],
        store=store,
        workspace=str(tmp_path),
        model="test",
        base_url="https://test.invalid",
    )
    for i in range(5):
        session.add(UserMessage(content=f"question {i}"))
        session.add(
            AssistantMessage(
                content=f"answer {i}", reasoning_content="r" * (300 if i == 4 else 2600)
            )
        )
    session.add(UserMessage(content="continue"))
    original = session.records
    without = LLMRequest(
        messages=tuple(
            m.model_copy(update={"reasoning_content": None})
            if isinstance(m, AssistantMessage)
            else m
            for m in session.messages
        )
    )
    assert estimate_tokens(LLMRequest(messages=session.messages)) > estimate_tokens(
        without
    )
    provider = SummaryProvider()
    asyncio.run(prepare(ContextManager(provider, 4096), session))
    assert provider.calls
    for request in provider.calls:
        assert request.thinking == "disabled"
        assert request.tools == ()
        assert all(
            "reasoning_content" not in m
            for m in json.loads(request.messages[1].content)["messages"]
        )
    assert session.summary
    assert all(message == dict(original)[seq] for seq, message in session.records)
    assert any(
        isinstance(m, AssistantMessage) and m.reasoning_content
        for _, m in session.records
    )
    session.finish()
    restored = Session.resume(
        session.id,
        store=store,
        workspace=str(tmp_path),
        model="test",
        base_url="https://test.invalid",
        require_reasoning_content=True,
    )
    assert restored.messages == session.messages


def test_large_reasoning_tool_group_is_not_split_to_fit_budget(store):
    session = Session(
        [SystemMessage(content="sys"), UserMessage(content="read")], store=store
    )
    session.add(
        AssistantMessage(
            reasoning_content="r" * 20000, tool_calls=(ToolCall(id="one", name="read"),)
        )
    )
    session.add(ToolMessage(tool_call_id="one", content="result"))
    before = session.records
    with pytest.raises(ContextError):
        asyncio.run(prepare(ContextManager(SummaryProvider(), 1024), session))
    assert session.records == before
    assert checkpoint_count(store) == 0


@pytest.mark.parametrize(
    "content", ["hello", "x" * 160000], ids=["small", "above_previous_trigger"]
)
def test_under_threshold_preserves_context_and_makes_no_summary_call(store, content):
    session = Session(
        [SystemMessage(content="sys"), UserMessage(content=content)], store=store
    )
    before = session.messages
    provider = SummaryProvider()
    assert asyncio.run(prepare(ContextManager(provider), session)) == []
    assert session.messages == before
    assert provider.calls == []
    assert checkpoint_count(store) == 0


def test_default_budget_compacts_without_dropping_a_large_read_page(store, tmp_path):
    path = tmp_path / "large.txt"
    path.write_text(("x" * 1900 + "\n") * 100, encoding="utf-8")
    read = ReadTool(tmp_path)
    output = read.execute({"path": path.name})
    assert len(output.encode()) > 39 * 1024

    session = Session([SystemMessage(content="original instructions")], store=store)
    for _ in range(12):
        session.add(UserMessage(content="old question " + "x" * 65000))
        session.add(AssistantMessage(content="old answer " + "y" * 65000))
    session.add(UserMessage(content="Inspect this file and continue."))
    call = ToolCall(id="read-page", name="read", arguments={"path": path.name})
    session.add(AssistantMessage(tool_calls=(call,)))
    result = ToolMessage(tool_call_id=call.id, content=output)
    session.add(result)
    tools = (read.definition(),)
    provider = SummaryProvider()
    manager = ContextManager(provider)
    assert estimate_tokens(LLMRequest(messages=session.messages, tools=tools)) >= (
        manager.budget * 0.75
    )

    events = asyncio.run(prepare(manager, session, tools))

    assert isinstance(events[-1], ContextCompacted)
    assert events[-1].after_tokens <= manager.budget * 0.5
    assert provider.calls
    assert session.messages[-2] == AssistantMessage(tool_calls=(call,))
    assert session.messages[-1] == result
    assert checkpoint_count(store) == 1


def test_incremental_batched_compaction_archives_originals_without_reloading(store):
    session = long_session(store)
    before = session.records
    provider = SummaryProvider()
    manager = ContextManager(provider, 4096)
    queries = []
    store._connection.set_trace_callback(queries.append)
    events = asyncio.run(prepare(manager, session))
    assert [type(event) for event in events] == [
        ContextCompactionStarted,
        ContextCompacted,
    ]
    assert events[-1].after_tokens <= 2048
    assert len(provider.calls) > 1
    assert not any("FROM MESSAGES" in query.upper() for query in queries)
    assert len(session.records) < len(before)
    assert session.messages[0] == before[0][1]
    assert session.messages[1].role == "user"
    assert "reference data" in session.messages[1].content
    assert session.messages[-1] == before[-1][1]
    removed_sequences = {seq for seq, _ in before} - {seq for seq, _ in session.records}
    summarized = []
    for index, request in enumerate(provider.calls):
        assert request.tools == ()
        assert estimate_tokens(request) <= manager.budget
        data = json.loads(request.messages[1].content)
        assert data["previous_summary"] == (None if index == 0 else f"summary {index}")
        summarized.extend(data["messages"])
    assert summarized == [
        m.model_dump(mode="json", exclude={"reasoning_content"})
        for seq, m in before
        if seq in removed_sequences
    ]
    previous_summary = session.summary
    original_call_count = len(provider.calls)
    for _ in range(4):
        session.add(UserMessage(content="new question " + "z" * 2000))
        session.add(AssistantMessage(content="new answer " + "q" * 2000))
    session.add(UserMessage(content="continue"))
    asyncio.run(prepare(manager, session))
    assert (
        json.loads(provider.calls[original_call_count].messages[1].content)[
            "previous_summary"
        ]
        == previous_summary
    )
    assert checkpoint_count(store) == 2
    assert (
        store._connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        == len(before) + 9
    )
    assert all(
        "old question 0" not in request.messages[1].content
        for request in provider.calls[original_call_count:]
    )


@pytest.mark.parametrize(
    "result",
    [
        LLMResponse(
            message=AssistantMessage(content=""), finish_reason=FinishReason.END_TURN
        ),
        LLMResponse(
            message=AssistantMessage(content="   "), finish_reason=FinishReason.END_TURN
        ),
        LLMResponse(
            message=AssistantMessage(content="partial"),
            finish_reason=FinishReason.MAX_TOKENS,
        ),
        LLMResponse(
            message=AssistantMessage(
                content="text", tool_calls=(ToolCall(id="x", name="read"),)
            ),
            finish_reason=FinishReason.END_TURN,
        ),
        LLMResponse(
            message=AssistantMessage(content="x" * 1000),
            finish_reason=FinishReason.END_TURN,
        ),
    ],
)
def test_invalid_summary_leaves_originals_and_checkpoint_unchanged(store, result):
    session = long_session(store)
    before = session.records
    provider = SummaryProvider(result=result)
    events = []

    async def run():
        async for event in ContextManager(provider, 4096).prepare(session, (), 1):
            events.append(event)

    with pytest.raises(ContextError):
        asyncio.run(run())
    assert [type(event) for event in events] == [ContextCompactionStarted]
    assert session.records == before
    assert session.summary is None
    assert checkpoint_count(store) == 0
    assert provider.closed == 1


@pytest.mark.parametrize("failure", [ModelError("offline"), asyncio.CancelledError()])
def test_summary_failure_and_cancellation_preserve_context(store, failure):
    session = long_session(store)
    before = session.records
    provider = SummaryProvider(failure=failure)
    expected = (
        ContextError if isinstance(failure, ModelError) else asyncio.CancelledError
    )
    with pytest.raises(expected):
        asyncio.run(prepare(ContextManager(provider, 4096), session))
    assert session.records == before
    assert session.summary is None
    assert checkpoint_count(store) == 0
    assert provider.closed == 1


def test_later_batch_failure_does_not_commit_partial_summary(store):
    session = long_session(store)
    before = session.records

    class LaterFailure(SummaryProvider):
        async def stream(self, request):
            if self.calls:
                self.failure = ModelError("second batch failed")
            async for event in super().stream(request):
                yield event

    provider = LaterFailure()
    with pytest.raises(ContextError, match="second batch failed"):
        asyncio.run(prepare(ContextManager(provider, 4096), session))
    assert session.records == before
    assert session.summary is None
    assert checkpoint_count(store) == 0


@pytest.mark.parametrize(
    "kind", ["latest_input", "system", "old_unit", "no_history", "tools"]
)
def test_uncompressible_inputs_fail_without_normal_request(store, kind):
    messages = [
        SystemMessage(content="sys"),
        AssistantMessage(content="old " + "x" * 10000),
        UserMessage(content="latest"),
    ]
    tools = ()
    if kind == "latest_input":
        messages[-1] = UserMessage(content="x" * 15000)
    elif kind == "system":
        messages[0] = SystemMessage(content="x" * 15000)
    elif kind == "old_unit":
        messages[1] = AssistantMessage(content="x" * 15000)
    elif kind == "no_history":
        messages = [SystemMessage(content="sys"), UserMessage(content="x" * 15000)]
    else:
        tools = (ToolDefinition(name="huge", description="x" * 15000, parameters={}),)
    session = Session(messages, store=store)
    before = session.records
    provider = SummaryProvider()
    with pytest.raises(ContextError):
        asyncio.run(prepare(ContextManager(provider, 4096), session, tools))
    assert provider.calls == []
    assert session.records == before
    assert checkpoint_count(store) == 0


def test_estimation_covers_tool_definitions_arguments_and_calibrates_per_session(store):
    session = Session(store=store)
    request = LLMRequest(messages=(UserMessage(content="hello"),))
    larger = LLMRequest(
        messages=(
            AssistantMessage(
                tool_calls=(
                    ToolCall(id="x", name="read", arguments={"content": "中文" * 100}),
                )
            ),
        ),
        tools=(ToolDefinition(name="read", description="x" * 1000, parameters={}),),
    )
    assert estimate_tokens(larger) > estimate_tokens(request)
    observe_usage(session, request, None)
    assert session.token_factor == 1.2
    observe_usage(
        session,
        request,
        TokenUsage(
            input_tokens=base_tokens(request) * 2,
            output_tokens=1,
            total_tokens=base_tokens(request) * 2 + 1,
        ),
    )
    assert session.token_factor == 2.4
    observe_usage(
        session,
        request,
        TokenUsage(input_tokens=1, output_tokens=9999, total_tokens=10000),
    )
    assert session.token_factor == 2.4
    assert Session(store=store).token_factor == 1.2


def test_tool_loop_compacts_between_complete_exchanges_without_spending_steps(store):
    class OutputTool(Tool):
        name = "output"
        description = "return data"
        parameters: ClassVar[dict] = {"type": "object", "properties": {}}
        executions = 0

        def execute(self, arguments):
            self.executions += 1
            return "x" * 3000

    class ToolProvider(SummaryProvider):
        normal_calls = 0

        async def stream(self, request):
            if not request.tools:
                async for event in super().stream(request):
                    yield event
                return
            self.normal_calls += 1
            if self.normal_calls <= 3:
                message = AssistantMessage(
                    tool_calls=tuple(
                        ToolCall(id=f"{self.normal_calls}-{i}", name="output")
                        for i in range(2)
                    )
                )
                reason = FinishReason.TOOL_CALLS
            else:
                assert request.messages[0] == SystemMessage(content="sys")
                assert "reference data" in request.messages[1].content
                assert UserMessage(content="original task") in request.messages
                assert [
                    m.tool_call_id
                    for m in request.messages
                    if isinstance(m, ToolMessage)
                ] == ["3-0", "3-1"]
                message = AssistantMessage(content="done")
                reason = FinishReason.END_TURN
            yield LLMResponseCompleted(
                response=LLMResponse(message=message, finish_reason=reason)
            )

    provider = ToolProvider()
    tool = OutputTool()
    loop = AgentLoop(
        Agent(provider=provider, tools=ToolRegistry([tool]), system_prompt="sys"),
        max_steps=4,
        context_budget=8192,
        permission_policy=PermissionPolicy(Path.cwd()),
        approve=approve_once,
    )
    session = Session(store=store)

    async def run():
        return [event async for event in loop.stream(session, "original task")]

    events = asyncio.run(run())
    assert tool.executions == 6
    assert provider.normal_calls == 4
    start = next(
        i for i, e in enumerate(events) if isinstance(e, ContextCompactionStarted)
    )
    assert isinstance(events[start - 1], ToolCompleted)
    assert isinstance(events[start + 1], ContextCompacted)
    assert isinstance(events[start + 2], ModelStarted)
    assert events[start + 2].step == 4
    assert not any(isinstance(e, AssistantTextDelta) for e in events)
    assert sum(isinstance(e, ModelCompleted) for e in events) == 4
    assert session.messages[-1].content == "done"
    assert checkpoint_count(store) == 1
    for request in provider.calls:
        data = json.loads(request.messages[1].content)["messages"]
        calls = {
            call["id"] for message in data for call in message.get("tool_calls", [])
        }
        results = {
            message["tool_call_id"] for message in data if message["role"] == "tool"
        }
        assert calls == results


def test_checkpoint_failure_emits_no_success_or_normal_model_event(store):
    session = long_session(store)
    before = session.records
    with store._connection:
        store._connection.execute("""CREATE TRIGGER reject_checkpoint BEFORE INSERT ON checkpoints
            BEGIN SELECT RAISE(ABORT, 'checkpoint failed'); END""")
    provider = SummaryProvider()
    loop = AgentLoop(
        Agent(provider=provider, tools=ToolRegistry(), system_prompt="sys"),
        context_budget=4096,
        permission_policy=PermissionPolicy(Path.cwd()),
    )
    events = []

    async def run():
        async for event in loop.stream(session, "new input"):
            events.append(event)

    with pytest.raises(StorageError):
        asyncio.run(run())
    assert [type(e) for e in events] == [TurnStarted, ContextCompactionStarted]
    assert session.records[:-1] == before
    assert session.summary is None
    assert checkpoint_count(store) == 0


def test_cancel_during_summary_preserves_previous_checkpoint(store):
    session = long_session(store)
    session.compact("previous summary", (session.records[0], session.records[-1]))
    for index in range(5):
        session.add(UserMessage(content=f"question {index}: " + "x" * 2000))
        session.add(AssistantMessage(content=f"answer {index}: " + "y" * 2000))
    session.add(UserMessage(content="latest"))
    previous = session.records
    entered = asyncio.Event()

    class PausedSummary(LLMProvider):
        closed = False

        async def stream(self, request):
            try:
                yield LLMTextDelta(delta="uncommitted summary")
                entered.set()
                await asyncio.Future()
            finally:
                self.closed = True

    provider = PausedSummary()
    loop = AgentLoop(
        Agent(provider=provider, tools=ToolRegistry(), system_prompt="sys"),
        context_budget=4096,
        permission_policy=PermissionPolicy(Path.cwd()),
    )
    events = []

    async def consume():
        async for event in loop.stream(session, "new input"):
            events.append(event)

    async def run():
        owner = asyncio.create_task(consume())
        await entered.wait()
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner

    asyncio.run(run())
    assert provider.closed
    assert session.summary == "previous summary"
    assert session.records[:-1] == previous
    assert checkpoint_count(store) == 1
    assert [type(e) for e in events] == [TurnStarted, ContextCompactionStarted]
    session.finish()


def test_summary_that_cannot_reach_target_is_not_committed(store):
    session = long_session(store)
    # Pin enough current input that a valid-sized summary exceeds the target.
    from codehead.session.session import context_messages

    content = "z"
    while True:
        candidate = ((1, session.records[0][1]), (100, UserMessage(content=content)))
        cost = estimate_tokens(LLMRequest(messages=context_messages(candidate, "")))
        if cost >= 2040:
            break
        content += "z" * 10
    session.add(UserMessage(content=content))
    before = session.records
    provider = SummaryProvider(
        result=LLMResponse(
            message=AssistantMessage(content="s" * 200),
            finish_reason=FinishReason.END_TURN,
        )
    )
    with pytest.raises(ContextError, match="did not reduce"):
        asyncio.run(prepare(ContextManager(provider, 4096), session))
    assert session.records == before
    assert session.summary is None
    assert checkpoint_count(store) == 0


@pytest.mark.parametrize("kind", ["missing", "wrong_id", "orphan", "duplicate"])
def test_malformed_tool_groups_cannot_be_compacted(store, kind):
    session = long_session(store)
    if kind == "orphan":
        session.add(ToolMessage(tool_call_id="missing", content="orphan"))
    else:
        calls = (ToolCall(id="one", name="read"),)
        if kind == "duplicate":
            calls = calls + calls
        session.add(AssistantMessage(tool_calls=calls))
        if kind == "wrong_id":
            session.add(ToolMessage(tool_call_id="other", content="wrong"))
    before = session.records
    provider = SummaryProvider()
    with pytest.raises(ContextError):
        asyncio.run(prepare(ContextManager(provider, 4096), session))
    assert session.records == before
    assert provider.calls == []


@pytest.mark.parametrize("failed_role", ["assistant", "tool"])
def test_message_archive_failure_prevents_completion_and_further_tools(
    store, failed_role
):
    class CountingTool(Tool):
        name = "count"
        description = "count executions"
        parameters: ClassVar[dict] = {}
        executions = 0

        def execute(self, arguments):
            self.executions += 1
            return "done"

    provider = SummaryProvider(
        result=LLMResponse(
            message=AssistantMessage(
                tool_calls=(
                    ToolCall(id="one", name="count"),
                    ToolCall(id="two", name="count"),
                )
            ),
            finish_reason=FinishReason.TOOL_CALLS,
        )
    )
    tool = CountingTool()
    session = Session(store=store)
    with store._connection:
        store._connection.execute(f"""CREATE TRIGGER reject_message BEFORE INSERT ON messages
            WHEN NEW.message_json LIKE '%"role":"{failed_role}"%'
            BEGIN SELECT RAISE(ABORT, 'message failed'); END""")
    loop = AgentLoop(
        Agent(provider=provider, tools=ToolRegistry([tool]), system_prompt="sys"),
        permission_policy=PermissionPolicy(Path.cwd()),
        approve=approve_once,
    )
    events = []

    async def run():
        async for event in loop.stream(session, "go"):
            events.append(event)

    with pytest.raises(StorageError):
        asyncio.run(run())
    assert tool.executions == (0 if failed_role == "assistant" else 1)
    assert not any(isinstance(event, ToolCompleted) for event in events)
    if failed_role == "assistant":
        assert not any(
            isinstance(event, (ModelCompleted, ToolStarted)) for event in events
        )
        assert [m.role for m in session.messages] == ["system", "user"]
    else:
        assert sum(isinstance(event, ModelCompleted) for event in events) == 1
        assert [m.role for m in session.messages] == ["system", "user", "assistant"]
