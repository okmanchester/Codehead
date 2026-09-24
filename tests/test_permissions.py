"""Permission rules, approvals, and archive compatibility."""

import asyncio
import signal
import sqlite3
from collections.abc import AsyncIterator, Mapping
from contextlib import aclosing
from io import StringIO
from typing import ClassVar

import pytest
from rich.console import Console

from codehead.agent import Agent, AgentLoop
from codehead.cli import run_repl
from codehead.llm import LLMProvider
from codehead.models import (
    AssistantMessage,
    FinishReason,
    JsonObject,
    JsonValue,
    LLMResponse,
    LLMResponseCompleted,
    LLMStreamEvent,
    ToolApprovalRequested,
    ToolCall,
    ToolCompleted,
    ToolMessage,
    ToolResult,
    ToolStarted,
    TurnCancelled,
    TurnFailed,
)
from codehead.permissions import PermissionPolicy, PermissionRule
from codehead.session import Session, SQLiteSessionStore
from codehead.tools import GrepTool, ReadTool, Tool, ToolRegistry, WriteTool


class Provider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = iter(responses)
        self.requests = []

    async def stream(self, request) -> AsyncIterator[LLMStreamEvent]:
        self.requests.append(request)
        yield LLMResponseCompleted(response=next(self.responses))


class RecordingWrite(Tool):
    name = "write"
    description = "record writes"
    parameters: ClassVar[JsonObject] = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def execute(self, arguments: Mapping[str, JsonValue]) -> str:
        self.calls.append(dict(arguments))
        return "written"


def tool_response(*calls: ToolCall) -> LLMResponse:
    return LLMResponse(
        message=AssistantMessage(tool_calls=calls),
        finish_reason=FinishReason.TOOL_CALLS,
    )


def answer() -> LLMResponse:
    return LLMResponse(
        message=AssistantMessage(content="done"), finish_reason=FinishReason.END_TURN
    )


def make_loop(tmp_path, tool, responses, *, policy=None, approve=None):
    provider = Provider(responses)
    loop = AgentLoop(
        Agent(provider=provider, tools=ToolRegistry([tool]), system_prompt="system"),
        permission_policy=policy or PermissionPolicy(tmp_path),
        approve=approve,
    )
    return loop, provider


def test_rule_order_defaults_and_parameter_matching(tmp_path):
    folder = tmp_path / ".codehead"
    folder.mkdir()
    (folder / "permissions.toml").write_text(
        """[[rules]]
tool = "write"
action = "deny"
path = "./private/*"

[[rules]]
tool = "write"
action = "allow"
path = "*"

[[rules]]
tool = "bash"
action = "allow"
command = "git status --short"
workdir = "src/*"
""",
        encoding="utf-8",
    )
    policy = PermissionPolicy.from_file(tmp_path, ["read", "write", "bash"])
    assert policy.action(ToolCall(id="1", name="read")) == "allow"
    assert (
        policy.action(
            ToolCall(id="2", name="write", arguments={"path": "private/secret"})
        )
        == "deny"
    )
    assert (
        policy.action(
            ToolCall(id="3", name="write", arguments={"path": "public/../public/a"})
        )
        == "allow"
    )
    assert (
        policy.action(
            ToolCall(
                id="4",
                name="bash",
                arguments={"command": "git status --short", "workdir": "src/codehead"},
            )
        )
        == "allow"
    )
    assert (
        policy.action(
            ToolCall(
                id="5",
                name="bash",
                arguments={"command": "git status", "workdir": "src/codehead"},
            )
        )
        == "ask"
    )


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('[[rules]]\ntool = "missing"\naction = "allow"', "unknown tool"),
        ('[[rules]]\ntool = "write"\naction = "execute"', "invalid action"),
        (
            '[[rules]]\ntool = "write"\naction = "allow"\ncommand = "x"',
            "unsupported fields",
        ),
        (
            '[[rules]]\ntool = "write"\naction = "allow"\npath = "../outside"',
            "invalid path",
        ),
        ("unexpected = 1", "only"),
    ],
)
def test_invalid_permission_file_rejected(tmp_path, content, message):
    folder = tmp_path / ".codehead"
    folder.mkdir()
    (folder / "permissions.toml").write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        PermissionPolicy.from_file(tmp_path, ["write"])


@pytest.mark.parametrize(("choice", "approval_count"), [("once", 2), ("session", 1)])
def test_approval_lifetime_for_same_tool_and_target(tmp_path, choice, approval_count):
    calls = (
        ToolCall(id="one", name="write", arguments={"path": "a.txt", "content": "one"}),
        ToolCall(
            id="two", name="write", arguments={"path": "./a.txt", "content": "two"}
        ),
    )
    approvals = []

    async def approve(call):
        approvals.append(call)
        return choice

    tool = RecordingWrite()
    loop, _ = make_loop(
        tmp_path, tool, [tool_response(*calls), answer()], approve=approve
    )
    session = Session()
    assert asyncio.run(loop.run(session, "write")) == "done"
    assert len(approvals) == approval_count
    assert [call["content"] for call in tool.calls] == ["one", "two"]


def test_denial_does_not_execute_and_is_archived_for_model(tmp_path):
    call = ToolCall(id="one", name="write", arguments={"path": "a.txt", "content": "x"})
    tool = RecordingWrite()
    loop, provider = make_loop(tmp_path, tool, [tool_response(call), answer()])
    session = Session()

    async def run():
        return [event async for event in loop.stream(session, "write")]

    events = asyncio.run(run())
    assert tool.calls == []
    assert any(isinstance(event, ToolApprovalRequested) for event in events)
    assert not any(isinstance(event, ToolStarted) for event in events)
    completion = next(event for event in events if isinstance(event, ToolCompleted))
    assert completion.result.outcome == "denied"
    assert completion.tool_name == "write"
    assert isinstance(provider.requests[1].messages[-1], ToolMessage)
    assert provider.requests[1].messages[-1].is_error
    with session._store._transaction() as connection:
        assert (
            connection.execute("SELECT status FROM tool_executions").fetchone()[0]
            == "denied"
        )


def test_policy_deny_overrides_session_grant(tmp_path):
    call = ToolCall(id="one", name="write", arguments={"path": "secret.txt"})
    policy = PermissionPolicy(
        tmp_path, [PermissionRule("write", "deny", path="secret.txt")]
    )
    session = Session()
    policy.grant(session, call)
    tool = RecordingWrite()
    loop, _ = make_loop(tmp_path, tool, [tool_response(call), answer()], policy=policy)
    assert asyncio.run(loop.run(session, "write")) == "done"
    assert tool.calls == []
    result = next(
        message for message in session.messages if isinstance(message, ToolMessage)
    )
    assert "permission policy" in result.content
    with session._store._transaction() as connection:
        assert (
            connection.execute("SELECT status FROM tool_executions").fetchone()[0]
            == "denied"
        )


def test_grant_target_and_resume_boundary(tmp_path):
    database = tmp_path / "sessions.sqlite3"
    store = SQLiteSessionStore(database)
    session = Session(
        store=store, workspace=str(tmp_path), model="model", base_url="url"
    )
    policy = PermissionPolicy(tmp_path)
    call = ToolCall(id="one", name="bash", arguments={"command": "pwd", "workdir": "."})
    policy.grant(session, call)
    assert policy.is_granted(session, call)
    assert not policy.is_granted(
        session,
        ToolCall(id="two", name="bash", arguments={"command": "ls", "workdir": "."}),
    )
    assert not policy.is_granted(
        session,
        ToolCall(
            id="three", name="bash", arguments={"command": "pwd", "workdir": "sub"}
        ),
    )
    session.finish()
    restored = Session.resume(
        session.id, store=store, workspace=str(tmp_path), model="model", base_url="url"
    )
    assert not policy.is_granted(restored, call)
    restored.finish()
    store.close()


def test_user_denial_is_remembered_only_for_current_turn(tmp_path):
    calls = (
        ToolCall(id="one", name="write", arguments={"path": "a.txt"}),
        ToolCall(id="two", name="write", arguments={"path": "a.txt"}),
    )
    later = ToolCall(id="three", name="write", arguments={"path": "a.txt"})
    prompts = []

    async def refuse(call):
        prompts.append(call)
        return "deny"

    tool = RecordingWrite()
    loop, _ = make_loop(
        tmp_path,
        tool,
        [tool_response(*calls), answer(), tool_response(later), answer()],
        approve=refuse,
    )
    session = Session()
    assert asyncio.run(loop.run(session, "write")) == "done"
    assert len(prompts) == 1
    assert asyncio.run(loop.run(session, "retry")) == "done"
    assert len(prompts) == 2
    assert tool.calls == []
    denied = [
        message for message in session.messages if isinstance(message, ToolMessage)
    ]
    assert len(denied) == 3
    assert all("Denied by user" in message.content for message in denied)


def test_session_approval_is_reused_across_turns(tmp_path):
    calls = (
        ToolCall(id="one", name="write", arguments={"path": "a.txt", "content": "one"}),
        ToolCall(
            id="two", name="write", arguments={"path": "./a.txt", "content": "two"}
        ),
    )
    approvals = []

    async def approve(call):
        approvals.append(call)
        return "session"

    tool = RecordingWrite()
    loop, _ = make_loop(
        tmp_path,
        tool,
        [tool_response(calls[0]), answer(), tool_response(calls[1]), answer()],
        approve=approve,
    )
    session = Session()
    assert asyncio.run(loop.run(session, "first")) == "done"
    assert asyncio.run(loop.run(session, "second")) == "done"
    assert len(approvals) == 1
    assert [call["content"] for call in tool.calls] == ["one", "two"]


def test_recursive_search_restrictive_rules_block_overlapping_roots(tmp_path):
    policy = PermissionPolicy(
        tmp_path, [PermissionRule("grep", "deny", path="secret/*")]
    )

    def call(path):
        return ToolCall(
            id=path, name="grep", arguments={"pattern": "TOKEN", "path": path}
        )

    assert policy.action(call(".")) == "deny"
    assert policy.action(call("secret")) == "deny"
    assert policy.action(call("secret/creds.txt")) == "deny"
    assert policy.action(call("public")) == "allow"
    assert (
        PermissionPolicy(
            tmp_path, [PermissionRule("grep", "ask", path="secret/*")]
        ).action(call("."))
        == "ask"
    )
    assert (
        PermissionPolicy(
            tmp_path, [PermissionRule("grep", "deny", path="se*ret/*")]
        ).action(call("public"))
        == "deny"
    )
    assert (
        PermissionPolicy(
            tmp_path,
            [
                PermissionRule("grep", "allow", path="."),
                PermissionRule("grep", "deny", path="secret/*"),
            ],
        ).action(call("."))
        == "allow"
    )
    assert (
        PermissionPolicy(
            tmp_path, [PermissionRule("glob", "deny", path="secret/*")]
        ).action(ToolCall(id="glob", name="glob", arguments={"pattern": "*"}))
        == "deny"
    )


def test_real_grep_does_not_expose_denied_subtree(tmp_path):
    secret = tmp_path / "secret"
    public = tmp_path / "public"
    secret.mkdir()
    public.mkdir()
    (secret / "creds.txt").write_text("TOKEN=super-secret-value\n", encoding="utf-8")
    (public / "info.txt").write_text("PUBLIC=ok\n", encoding="utf-8")
    calls = (
        ToolCall(id="root", name="grep", arguments={"pattern": "TOKEN", "path": "."}),
        ToolCall(
            id="secret", name="grep", arguments={"pattern": "TOKEN", "path": "secret"}
        ),
        ToolCall(
            id="public", name="grep", arguments={"pattern": "PUBLIC", "path": "public"}
        ),
    )
    policy = PermissionPolicy(
        tmp_path, [PermissionRule("grep", "deny", path="secret/*")]
    )
    loop, provider = make_loop(
        tmp_path, GrepTool(tmp_path), [tool_response(*calls), answer()], policy=policy
    )
    session = Session()

    async def run():
        return [event async for event in loop.stream(session, "search")]

    events = asyncio.run(run())
    results = [event.result for event in events if isinstance(event, ToolCompleted)]
    assert [result.outcome for result in results] == ["denied", "denied", "succeeded"]
    assert "PUBLIC=ok" in results[2].output
    assert "super-secret-value" not in str(provider.requests[1].messages)


@pytest.mark.parametrize("name", ["read", "write"])
def test_invalid_path_returns_tool_result_instead_of_failing_turn(tmp_path, name):
    tool = ReadTool(tmp_path) if name == "read" else WriteTool(tmp_path)
    arguments = {"path": "bad" + chr(0) + "name"}
    if name == "write":
        arguments["content"] = "x"
    call = ToolCall(id="invalid", name=name, arguments=arguments)

    async def approve(call):
        return "once"

    loop, _ = make_loop(
        tmp_path, tool, [tool_response(call), answer()], approve=approve
    )
    session = Session()

    async def run():
        return [event async for event in loop.stream(session, "invalid path")]

    events = asyncio.run(run())
    assert not any(isinstance(event, TurnFailed) for event in events)
    result = next(event.result for event in events if isinstance(event, ToolCompleted))
    assert result.outcome == "failed"
    assert PermissionPolicy(tmp_path).denial_key(call)[1] == "<invalid target>"


def test_outside_and_missing_paths_cannot_get_session_grants(tmp_path):
    policy = PermissionPolicy(
        tmp_path, [PermissionRule("read", "deny", path="secret/*")]
    )
    session = Session()
    outside = ToolCall(
        id="outside", name="read", arguments={"path": str(tmp_path.parent / "outside")}
    )
    missing = ToolCall(id="missing", name="write", arguments={"content": "x"})
    assert policy.action(outside) == "allow"
    assert policy.action(missing) == "ask"
    policy.grant(session, outside)
    policy.grant(session, missing)
    assert not policy.is_granted(session, outside)
    assert not policy.is_granted(session, missing)


def test_agent_loop_requires_matching_permission_workspace(tmp_path):
    provider = Provider([answer()])
    agent = Agent(
        provider=provider,
        tools=ToolRegistry([ReadTool(tmp_path)]),
        system_prompt="system",
    )
    with pytest.raises(TypeError, match="permission_policy"):
        AgentLoop(agent)
    with pytest.raises(TypeError, match="permission_policy"):
        AgentLoop(agent, permission_policy=None)
    with pytest.raises(ValueError, match="Permission workspace"):
        AgentLoop(agent, permission_policy=PermissionPolicy(tmp_path / "other"))


def test_agent_loop_checks_custom_tool_workspace_when_present(tmp_path):
    class WorkspaceTool(RecordingWrite):
        name = "workspace_tool"

        def __init__(self, workspace):
            super().__init__()
            self.workspace = workspace

    policy = PermissionPolicy(tmp_path)
    provider = Provider([answer()])

    def agent_with(tool):
        return Agent(
            provider=provider,
            tools=ToolRegistry([tool]),
            system_prompt="system",
        )

    AgentLoop(agent_with(WorkspaceTool(policy.workspace)), permission_policy=policy)
    with pytest.raises(ValueError, match="workspace_tool"):
        AgentLoop(
            agent_with(WorkspaceTool(tmp_path / "other")),
            permission_policy=policy,
        )
    AgentLoop(agent_with(RecordingWrite()), permission_policy=policy)


def test_cancel_while_waiting_for_approval_skips_unstarted_tool(tmp_path):
    entered = asyncio.Event()

    async def approve(call):
        entered.set()
        await asyncio.Future()

    tool = RecordingWrite()
    call = ToolCall(id="one", name="write", arguments={"path": "a.txt"})
    loop, _ = make_loop(tmp_path, tool, [tool_response(call)], approve=approve)
    session = Session()
    events = []

    async def consume():
        async with aclosing(loop.stream(session, "write")) as stream:
            async for event in stream:
                events.append(event)

    async def run():
        owner = asyncio.create_task(consume())
        await entered.wait()
        with pytest.raises(RuntimeError, match="active turn"):
            loop.set_approval_callback(None)
        assert loop.request_cancel(session)
        await owner

    asyncio.run(run())
    assert tool.calls == []
    assert session.turn.status == "cancelled"
    assert session.turn.tools[0].status == "skipped"
    assert any(isinstance(event, TurnCancelled) for event in events)


def test_cli_approval_shows_full_arguments_and_session_choice(tmp_path):
    tool = RecordingWrite()
    call = ToolCall(
        id="one",
        name="write",
        arguments={"path": "a.txt", "content": "complete content"},
    )
    loop, _ = make_loop(tmp_path, tool, [tool_response(call), answer()])
    output = StringIO()
    inputs = iter(["write", "s", "quit"])
    console = Console(file=output, force_terminal=False)
    assert (
        asyncio.run(run_repl(loop, console=console, read_input=lambda _: next(inputs)))
        == 0
    )
    assert tool.calls == [call.arguments]
    assert "complete content" in output.getvalue()
    assert "write: succeeded" in output.getvalue()


@pytest.mark.parametrize(
    ("response", "expected_outcome"),
    [("y", "succeeded"), ("n", "denied"), ("invalid", "succeeded")],
)
def test_cli_approval_choices_and_invalid_input(tmp_path, response, expected_outcome):
    tool = RecordingWrite()
    call = ToolCall(id="one", name="write", arguments={"path": "a.txt"})
    loop, _ = make_loop(tmp_path, tool, [tool_response(call), answer()])
    output = StringIO()
    inputs = iter(
        ["write", response, *(["y"] if response == "invalid" else []), "quit"]
    )
    assert (
        asyncio.run(
            run_repl(
                loop, console=Console(file=output), read_input=lambda _: next(inputs)
            )
        )
        == 0
    )
    assert f"write: {expected_outcome}" in output.getvalue()
    assert bool(tool.calls) is (expected_outcome == "succeeded")
    if response == "invalid":
        assert "Enter y, s, or n." in output.getvalue()


def test_cli_keyboard_interrupt_during_approval_cancels_turn(tmp_path):
    tool = RecordingWrite()
    call = ToolCall(id="one", name="write", arguments={"path": "a.txt"})
    loop, _ = make_loop(tmp_path, tool, [tool_response(call)])
    output = StringIO()
    inputs = iter(["write", "quit"])

    def reader(prompt):
        if "Allow once" in prompt:
            signal.raise_signal(signal.SIGINT)
        return next(inputs)

    assert (
        asyncio.run(run_repl(loop, console=Console(file=output), read_input=reader))
        == 0
    )
    assert tool.calls == []
    assert "当前回合已取消" in output.getvalue()


def test_existing_tool_executions_survive_denied_status_migration(tmp_path):
    database = tmp_path / "sessions.sqlite3"
    store = SQLiteSessionStore(database)
    session = Session(store=store)
    turn = session.begin_turn("go", "system")
    session.accept_response(
        turn, AssistantMessage(tool_calls=(ToolCall(id="one", name="read"),))
    )
    session.complete_tool(
        turn, turn.tools[0], ToolResult(tool_call_id="one", output="ok")
    )
    session.end_turn(turn, "failed", "test")
    session.finish()
    unfinished = Session(store=store)
    pending_turn = unfinished.begin_turn("later", "system")
    unfinished.accept_response(
        pending_turn,
        AssistantMessage(tool_calls=(ToolCall(id="pending", name="read"),)),
    )
    store.close()

    with sqlite3.connect(database) as connection:
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'tool_executions'"
        ).fetchone()[0]
        connection.execute("ALTER TABLE tool_executions RENAME TO tool_executions_new")
        connection.execute(schema.replace(",'denied'", ""))
        connection.execute(
            "INSERT INTO tool_executions SELECT * FROM tool_executions_new"
        )
        connection.execute("DROP TABLE tool_executions_new")

    migrated = SQLiteSessionStore(database)
    with migrated._transaction() as connection:
        assert sorted(
            row[0] for row in connection.execute("SELECT status FROM tool_executions")
        ) == ["pending", "succeeded"]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            "'denied'"
            in connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'tool_executions'"
            ).fetchone()[0]
        )
    new_session = Session(store=migrated)
    new_turn = new_session.begin_turn("denied", "system")
    new_session.accept_response(
        new_turn, AssistantMessage(tool_calls=(ToolCall(id="denied", name="read"),))
    )
    new_session.complete_tool(
        new_turn,
        new_turn.tools[0],
        ToolResult(tool_call_id="denied", output="denied", outcome="denied"),
    )
    new_session.end_turn(new_turn, "failed", "test")
    with migrated._transaction() as connection:
        assert (
            connection.execute(
                "SELECT status FROM tool_executions WHERE tool_call_id = 'denied'"
            ).fetchone()[0]
            == "denied"
        )
    migrated.close()
