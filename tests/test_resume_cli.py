import asyncio
import sqlite3
from contextlib import closing
from io import StringIO
from pathlib import Path
from typing import ClassVar
from unittest.mock import Mock, patch

import pytest
from rich.console import Console

from codehead.agent import Agent, AgentLoop
from codehead.cli import _main, _parse_args, run_repl
from codehead.config import DEFAULT_MAX_STEPS, RuntimeConfig
from codehead.config_loader import load_local_config
from codehead.errors import (
    AgentAbortedError,
    ContextError,
    ModelError,
    ResumeError,
    StorageError,
)
from codehead.llm import LLMProvider
from codehead.models import (
    AssistantMessage,
    FinishReason,
    LLMResponse,
    LLMResponseCompleted,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from codehead.permissions import PermissionPolicy, PermissionRule
from codehead.session import Session, SQLiteSessionStore
from codehead.tools import Tool, ToolRegistry


class Provider(LLMProvider):
    def __init__(self, responses=()):
        self.responses = iter(responses)
        self.calls = []
        self.closed = False

    async def stream(self, request):
        self.calls.append(request)
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        yield LLMResponseCompleted(response=response)

    async def aclose(self):
        self.closed = True


class CountingTool(Tool):
    name = "count"
    description = "count executions"
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}

    def __init__(self):
        self.executions = 0

    def execute(self, arguments):
        self.executions += 1
        return "done"


def loop_for(
    provider, *, tools=None, prompt="saved instructions", max_steps=DEFAULT_MAX_STEPS
):
    registry = tools or ToolRegistry()
    return AgentLoop(
        Agent(provider=provider, tools=registry, system_prompt=prompt),
        max_steps=max_steps,
        permission_policy=PermissionPolicy(
            Path.cwd(),
            [
                PermissionRule(definition.name, "allow")
                for definition in registry.definitions()
            ],
        ),
    )


def answer(text="answer"):
    return LLMResponse(
        message=AssistantMessage(content=text), finish_reason=FinishReason.END_TURN
    )


def test_main_rejects_missing_reasoning_before_accepting_input(setup):
    config, store, session = setup
    session.add(UserMessage(content="old"))
    session.add(AssistantMessage(content="legacy"))
    session.finish()
    provider = Provider()
    console = Console(file=StringIO())
    console.input = Mock(side_effect=AssertionError("must not read input"))
    config = config.model_copy(update={"thinking": "enabled"})
    with (
        patch("codehead.cli.load_runtime_config", return_value=config),
        patch("codehead.cli.build_agent_loop", return_value=loop_for(provider)),
        patch("codehead.cli.Console", return_value=console),
    ):
        assert asyncio.run(_main(["--resume", session.id])) == 1
    console.input.assert_not_called()
    assert provider.calls == []
    assert state(store, session) == "closed"
    assert "disable thinking" in console.file.getvalue()


def tool_response():
    return LLMResponse(
        message=AssistantMessage(tool_calls=(ToolCall(id="count", name="count"),)),
        finish_reason=FinishReason.TOOL_CALLS,
    )


@pytest.fixture
def setup(tmp_path):
    config = RuntimeConfig(
        api_key="test",
        workspace=tmp_path,
        model="test-model",
        base_url="https://test.invalid",
        thinking="disabled",
    )
    store = SQLiteSessionStore(config.session_db_path)
    session = Session(
        store=store,
        workspace=str(tmp_path),
        model=config.model,
        base_url=config.base_url,
    )
    yield config, store, session
    store.close()


def state(store, session):
    with store._transaction() as connection:
        return connection.execute(
            "SELECT status FROM sessions WHERE id=?", (session.id,)
        ).fetchone()[0]


@pytest.mark.parametrize(
    "exit_input", ["exit", "quit", EOFError(), KeyboardInterrupt()]
)
def test_only_prompt_exit_marks_session_closed(setup, exit_input):
    _, store, session = setup
    provider = Provider([answer()])
    inputs = iter(["hello", exit_input])

    def read(_):
        value = next(inputs)
        if isinstance(value, BaseException):
            raise value
        return value

    result = asyncio.run(
        run_repl(
            loop_for(provider),
            session=session,
            console=Console(file=StringIO()),
            read_input=read,
        )
    )
    assert result == 0
    assert state(store, session) == "closed"
    assert len(provider.calls) == 1


@pytest.mark.parametrize(
    "failure",
    [
        ModelError("model failed"),
        ContextError("summary failed"),
        AgentAbortedError("steps exhausted"),
    ],
)
def test_failed_turn_can_exit_and_resume_without_automatic_retry(setup, failure):
    config, store, session = setup
    provider = Provider([failure])
    inputs = iter(["unanswered", "exit"])
    output = StringIO()
    assert (
        asyncio.run(
            run_repl(
                loop_for(provider),
                session=session,
                console=Console(file=output),
                read_input=lambda _: next(inputs),
            )
        )
        == 0
    )
    expected = session.messages
    restored = Session.resume(
        session.id,
        store=store,
        workspace=str(config.workspace),
        model=config.model,
        base_url=config.base_url,
    )
    assert restored.messages == expected
    assert restored.messages[-1] == UserMessage(content="unanswered")
    assert len(provider.calls) == 1
    assert str(failure) in output.getvalue()


def test_actual_step_exhaustion_resumes_complete_tool_results_without_reexecution(
    setup,
):
    config, store, session = setup
    provider = Provider([tool_response()])
    tool = CountingTool()
    inputs = iter(["do work", "quit"])
    result = asyncio.run(
        run_repl(
            loop_for(provider, tools=ToolRegistry([tool]), max_steps=1),
            session=session,
            console=Console(file=StringIO()),
            read_input=lambda _: next(inputs),
        )
    )
    assert result == 0
    with store._transaction() as connection:
        assert (
            connection.execute(
                "SELECT status FROM turns WHERE session_id = ?", (session.id,)
            ).fetchone()[0]
            == "failed"
        )
    restored = Session.resume(
        session.id,
        store=store,
        workspace=str(config.workspace),
        model=config.model,
        base_url=config.base_url,
    )
    assert isinstance(restored.messages[-1], ToolMessage)
    assert tool.executions == 1
    next_provider = Provider([answer("continued")])
    assert (
        asyncio.run(
            loop_for(next_provider, tools=ToolRegistry([tool]), max_steps=1).run(
                restored, "continue"
            )
        )
        == "continued"
    )
    assert tool.executions == 1
    assert len(next_provider.calls) == 1


def test_actual_compaction_failure_restores_previous_summary_and_active_messages(setup):
    config, store, session = setup
    session.add(SystemMessage(content="saved instructions"))
    session.add(UserMessage(content="old question"))
    session.add(AssistantMessage(content="old answer"))
    session.add(UserMessage(content="current task"))
    session.compact("previous summary", (session.records[0], session.records[-1]))
    for _ in range(4):
        session.add(UserMessage(content="x" * 2000))
        session.add(AssistantMessage(content="y" * 2000))
    before = session.records
    provider = Provider([ModelError("summary unavailable")])
    loop = AgentLoop(
        Agent(
            provider=provider, tools=ToolRegistry(), system_prompt="saved instructions"
        ),
        context_budget=4096,
        permission_policy=PermissionPolicy(config.workspace),
    )
    inputs = iter(["continue", "exit"])
    assert (
        asyncio.run(
            run_repl(
                loop,
                session=session,
                console=Console(file=StringIO()),
                read_input=lambda _: next(inputs),
            )
        )
        == 0
    )
    restored = Session.resume(
        session.id,
        store=store,
        workspace=str(config.workspace),
        model=config.model,
        base_url=config.base_url,
    )
    assert restored.records[:-1] == before
    assert restored.records[-1][1] == UserMessage(content="continue")
    assert restored.summary == "previous summary"
    assert len(provider.calls) == 1
    assert (
        provider.calls[0]
        .messages[0]
        .content.startswith("Summarize earlier conversation")
    )


@pytest.mark.parametrize(
    "failure",
    [KeyboardInterrupt(), asyncio.CancelledError(), StorageError("disk failed")],
)
def test_controlled_interrupt_returns_to_prompt_but_storage_failure_is_fatal(
    setup, failure
):
    config, store, session = setup
    provider = Provider([failure])
    inputs = iter(["hello", "exit"])
    coroutine = run_repl(
        loop_for(provider),
        session=session,
        console=Console(file=StringIO()),
        read_input=lambda _: next(inputs),
    )
    fatal = isinstance(failure, StorageError)
    assert asyncio.run(coroutine) == (1 if fatal else 0)
    assert state(store, session) == ("active" if fatal else "closed")
    if fatal:
        with pytest.raises(ResumeError):
            Session.resume(
                session.id,
                store=store,
                workspace=str(config.workspace),
                model=config.model,
                base_url=config.base_url,
            )
    else:
        restored = Session.resume(
            session.id,
            store=store,
            workspace=str(config.workspace),
            model=config.model,
            base_url=config.base_url,
        )
        assert restored.messages == session.messages


def test_incomplete_tool_history_refuses_prompt_exit(setup):
    _, store, session = setup
    session.add(AssistantMessage(tool_calls=(ToolCall(id="missing", name="count"),)))
    output = StringIO()
    assert (
        asyncio.run(
            run_repl(
                loop_for(Provider()),
                session=session,
                console=Console(file=output),
                read_input=lambda _: "exit",
            )
        )
        == 1
    )
    assert "missing tool results" in output.getvalue()
    assert state(store, session) == "active"


def test_exit_commit_failure_is_reported_and_not_marked_closed(setup):
    config, store, session = setup
    with store._transaction() as connection:
        connection.execute("""CREATE TRIGGER reject_exit BEFORE UPDATE OF status ON sessions
            WHEN NEW.status='closed' BEGIN SELECT RAISE(ABORT, 'exit write failed'); END""")
    output = StringIO()
    assert (
        asyncio.run(
            run_repl(
                loop_for(Provider()),
                session=session,
                console=Console(file=output),
                read_input=lambda _: "quit",
            )
        )
        == 1
    )
    assert "exit write failed" in output.getvalue()
    assert not session._finished
    with closing(sqlite3.connect(config.session_db_path)) as reader:
        assert (
            reader.execute(
                "SELECT status FROM sessions WHERE id=?", (session.id,)
            ).fetchone()[0]
            == "active"
        )


@pytest.mark.parametrize("continue_conversation", [False, True])
def test_main_resume_uses_saved_prompt_current_model_and_waits_for_input(
    setup, continue_conversation
):
    config, store, session = setup
    session.add(SystemMessage(content="archived system"))
    session.add(UserMessage(content="old question"))
    session.add(AssistantMessage(content="old answer"))
    session.token_factor = 2.5
    session.finish()
    config = config.model_copy(update={"model": "new-model", "context_budget": 8192})
    provider = Provider([answer("new answer")])
    loop = loop_for(provider, prompt="current system")
    output = StringIO()
    console = Console(file=output, width=160)
    console.input = Mock(
        side_effect=["new question", "quit"] if continue_conversation else ["quit"]
    )
    with (
        patch("codehead.cli.load_runtime_config", return_value=config),
        patch("codehead.cli.build_agent_loop", return_value=loop),
        patch("codehead.cli.build_system_prompt", return_value="current system"),
        patch("codehead.cli.Console", return_value=console),
    ):
        assert asyncio.run(_main(["--resume", session.id])) == 0
    assert provider.closed
    assert len(provider.calls) == int(continue_conversation)
    assert state(store, session) == "closed"
    assert len(store.list_sessions(str(config.workspace))) == 1
    rendered = output.getvalue()
    assert f"Resumed session: {session.id}" in rendered
    assert "Model or endpoint changed" in rendered
    assert "System instructions changed" in rendered
    assert "old answer" not in rendered
    if continue_conversation:
        assert provider.calls[0].messages == (
            SystemMessage(content="archived system"),
            UserMessage(content="old question"),
            AssistantMessage(content="old answer"),
            UserMessage(content="new question"),
        )
    with store._transaction() as connection:
        assert connection.execute(
            "SELECT model, token_factor FROM sessions WHERE id=?", (session.id,)
        ).fetchone() == ("new-model", 1.2)


def test_main_resume_of_empty_session_uses_current_system_on_first_input(setup):
    config, _, session = setup
    session.finish()
    provider = Provider([answer()])
    console = Console(file=StringIO())
    console.input = Mock(side_effect=["hello", "exit"])
    with (
        patch("codehead.cli.load_runtime_config", return_value=config),
        patch(
            "codehead.cli.build_agent_loop",
            return_value=loop_for(provider, prompt="current"),
        ),
        patch("codehead.cli.build_system_prompt", return_value="current"),
        patch("codehead.cli.Console", return_value=console),
    ):
        assert asyncio.run(_main(["--resume", session.id])) == 0
    assert provider.calls[0].messages == (
        SystemMessage(content="current"),
        UserMessage(content="hello"),
    )


@pytest.mark.parametrize("session_id", ["not-an-id", ""])
def test_main_invalid_resume_does_not_create_a_session_and_closes_resources(
    setup, session_id
):
    config, store, session = setup
    session.finish()
    provider = Provider()
    with (
        patch("codehead.cli.load_runtime_config", return_value=config),
        patch("codehead.cli.build_agent_loop", return_value=loop_for(provider)),
        patch("codehead.cli.run_repl", side_effect=AssertionError("must not run")),
    ):
        assert asyncio.run(_main(["--resume", session_id])) == 1
    assert provider.closed
    assert provider.calls == []
    assert len(store.list_sessions(str(config.workspace))) == 1
    assert state(store, session) == "closed"


def test_default_start_creates_a_new_session_instead_of_resuming(setup):
    config, store, original = setup
    original.finish()
    provider = Provider()
    console = Console(file=StringIO())
    console.input = Mock(return_value="exit")
    with (
        patch("codehead.cli.load_runtime_config", return_value=config),
        patch("codehead.cli.build_agent_loop", return_value=loop_for(provider)),
        patch("codehead.cli.Console", return_value=console),
    ):
        assert asyncio.run(_main([])) == 0
    sessions = store.list_sessions(str(config.workspace))
    assert len(sessions) == 2
    assert all(row.status == "closed" for row in sessions)
    assert "New session:" in console.file.getvalue()


@pytest.mark.parametrize("has_archive", [False, True])
def test_list_sessions_needs_no_credentials_tools_or_model_config(
    tmp_path, monkeypatch, has_archive
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("codehead.config_loader.load_dotenv", lambda: None)
    monkeypatch.setenv("CODEHEAD_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("CODEHEAD_SESSION_DB", "")
    monkeypatch.setenv("CODEHEAD_CONTEXT_BUDGET", "invalid but irrelevant")
    (tmp_path / ".codehead").mkdir()
    (tmp_path / ".codehead" / "config.toml").write_text(
        'session_db = "sessions.sqlite3"\ncontext_budget = "invalid"\nmax_steps = false',
        encoding="utf-8",
    )
    local = load_local_config()
    session_id = None
    if has_archive:
        store = SQLiteSessionStore(local.session_db_path)
        session = Session(store=store, workspace=str(tmp_path), model="listed-model")
        session_id = session.id
        session.finish()
        store.close()
    output = StringIO()
    with (
        patch(
            "codehead.cli.load_runtime_config",
            side_effect=AssertionError("model config not needed"),
        ),
        patch(
            "codehead.cli.build_agent_loop",
            side_effect=AssertionError("no model or tools"),
        ),
        patch("codehead.cli.Console", return_value=Console(file=output, width=160)),
    ):
        assert asyncio.run(_main(["--list-sessions"])) == 0
    if has_archive:
        assert session_id in output.getvalue()
        assert "listed-model" in output.getvalue()
        assert "closed" in output.getvalue()
        with closing(sqlite3.connect(local.session_db_path)) as reader:
            assert reader.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    else:
        assert "No sessions" in output.getvalue()
        assert not local.session_db_path.exists()


@pytest.mark.parametrize("argv", [["--resume"], ["--resume", "id", "--list-sessions"]])
def test_resume_requires_id_and_excludes_list_mode(argv):
    with pytest.raises(SystemExit) as error:
        _parse_args(argv)
    assert error.value.code == 2


def test_cli_resume_uses_current_project_configuration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("codehead.config_loader.load_dotenv", lambda: None)
    for name in (
        "CODEHEAD_MODEL",
        "DEEPSEEK_BASE_URL",
        "CODEHEAD_SESSION_DB",
        "CODEHEAD_CONTEXT_BUDGET",
        "CODEHEAD_MAX_STEPS",
        "CODEHEAD_THINKING",
        "CODEHEAD_REASONING_EFFORT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CODEHEAD_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    database = tmp_path / ".codehead" / "sessions.sqlite3"
    store = SQLiteSessionStore(database)
    saved = Session(
        store=store,
        workspace=str(tmp_path),
        model="old-model",
        base_url="https://old.test",
    )
    turn = saved.begin_turn("first", "saved instructions")
    saved.accept_response(turn, answer().message)
    saved.finish()
    store.close()
    (tmp_path / ".codehead" / "config.toml").write_text(
        'model = "new-model"\nbase_url = "https://new.test"\n'
        'context_budget = 8192\nmax_steps = 3\nthinking = "disabled"',
        encoding="utf-8",
    )
    provider = Provider([answer("next answer")])
    captured = []

    def build(config, **kwargs):
        captured.append(config)
        return loop_for(provider, max_steps=config.max_steps)

    async def repl(loop, *, console, session):
        assert session.id == saved.id
        assert session.model_changed
        assert await loop.run(session, "next") == "next answer"
        session.finish()
        return 0

    with (
        patch("codehead.cli.build_agent_loop", side_effect=build),
        patch("codehead.cli.run_repl", side_effect=repl),
        patch("codehead.cli.Console", return_value=Console(file=StringIO())),
    ):
        assert asyncio.run(_main(["--resume", saved.id])) == 0
    assert captured[0].model == "new-model"
    assert captured[0].base_url == "https://new.test"
    assert captured[0].context_budget == 8192
    assert captured[0].max_steps == 3
    assert captured[0].thinking == "disabled"
