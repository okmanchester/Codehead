"""Deterministic interruption and archive boundary tests."""

import asyncio
import signal
import sqlite3
import threading
from contextlib import aclosing
from io import StringIO
from pathlib import Path
from typing import ClassVar

import pytest
from rich.console import Console

from codehead.agent import Agent, AgentLoop
from codehead.cli import run_repl
from codehead.errors import ResumeError, StorageError
from codehead.llm import LLMProvider
from codehead.models import (
    AssistantMessage,
    AssistantTextDelta,
    FinishReason,
    LLMResponse,
    LLMResponseCompleted,
    LLMTextDelta,
    ModelCompleted,
    ToolCall,
    ToolMessage,
    TurnCancelled,
    TurnCompleted,
    TurnFailed,
)
from codehead.session import Session, SQLiteSessionStore
from codehead.tools import Tool, ToolRegistry


class Provider(LLMProvider):
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0
        self.closed = 0

    async def stream(self, request):
        self.calls += 1
        try:
            response = next(self.responses)
            if isinstance(response, Exception):
                raise response
            yield LLMResponseCompleted(response=response)
        finally:
            self.closed += 1


class BarrierTool(Tool):
    name = "barrier"
    description = "controlled file write"
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}

    def __init__(self, path: Path):
        self.path = path
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def execute(self, arguments):
        self.calls += 1
        self.started.set()
        assert self.release.wait(timeout=5)
        self.path.write_text("written", encoding="utf-8")
        return "wrote file"


def tools_response(*ids):
    return LLMResponse(
        message=AssistantMessage(
            tool_calls=tuple(ToolCall(id=i, name="barrier") for i in ids)
        ),
        finish_reason=FinishReason.TOOL_CALLS,
    )


def answer():
    return LLMResponse(
        message=AssistantMessage(content="done"), finish_reason=FinishReason.END_TURN
    )


def make_loop(provider, tool=None, *, max_steps=4):
    return AgentLoop(
        Agent(
            provider=provider,
            tools=ToolRegistry([tool] if tool else []),
            system_prompt="system",
        ),
        max_steps=max_steps,
    )


def rows(store, table, session_id):
    with store._transaction() as connection:
        return connection.execute(
            f"SELECT * FROM {table} WHERE session_id = ? ORDER BY rowid", (session_id,)
        ).fetchall()


def test_begin_response_result_and_final_are_atomic_and_resume(tmp_path):
    store = SQLiteSessionStore(tmp_path / "history.sqlite3")
    session = Session(
        store=store, workspace=str(tmp_path), model="model", base_url="url"
    )
    tool = BarrierTool(tmp_path / "effect.txt")
    tool.release.set()
    provider = Provider([tools_response("same"), answer()])
    events = []

    async def run():
        async for event in make_loop(provider, tool).stream(session, "first"):
            events.append(event)

    asyncio.run(run())
    assert isinstance(events[-1], TurnCompleted)
    assert all(event.turn_id == session.turn.id for event in events)
    assert tool.path.read_text(encoding="utf-8") == "written"
    assert [r[2] for r in rows(store, "turns", session.id)] == ["completed"]
    execution = rows(store, "tool_executions", session.id)[0]
    assert execution[6] == "succeeded"
    assert execution[7] == 4
    assert [r[1] for r in rows(store, "messages", session.id)] == [1, 2, 3, 4, 5]
    session.finish()
    restored = Session.resume(
        session.id, store=store, workspace=str(tmp_path), model="model", base_url="url"
    )
    assert restored.messages == session.messages
    assert asyncio.run(make_loop(Provider([answer()])).run(restored, "next")) == "done"
    assert [r[1] for r in rows(store, "messages", session.id)] == list(range(1, 8))
    store.close()


def test_close_after_tool_response_skips_all_without_executing(tmp_path):
    store = SQLiteSessionStore(tmp_path / "history.sqlite3")
    session = Session(
        store=store, workspace=str(tmp_path), model="model", base_url="url"
    )
    tool = BarrierTool(tmp_path / "effect.txt")

    async def run():
        events = make_loop(Provider([tools_response("one", "two")]), tool).stream(
            session, "first"
        )
        async with aclosing(events):
            while not isinstance(await anext(events), ModelCompleted):
                pass

    asyncio.run(run())
    assert tool.calls == 0
    assert [r[6] for r in rows(store, "tool_executions", session.id)] == [
        "skipped",
        "skipped",
    ]
    assert [r[2] for r in rows(store, "turns", session.id)] == ["cancelled"]
    assert [
        msg.is_error for msg in session.messages if isinstance(msg, ToolMessage)
    ] == [True, True]
    session.finish()
    Session.resume(
        session.id, store=store, workspace=str(tmp_path), model="model", base_url="url"
    )
    store.close()


def test_cancel_during_file_tool_waits_for_true_result_then_skips_rest(tmp_path):
    store = SQLiteSessionStore(tmp_path / "history.sqlite3")
    session = Session(
        store=store, workspace=str(tmp_path), model="model", base_url="url"
    )
    tool = BarrierTool(tmp_path / "effect.txt")
    loop = make_loop(Provider([tools_response("one", "two")]), tool)
    events = []

    async def consume():
        async for event in loop.stream(session, "first"):
            events.append(event)

    async def run():
        owner = asyncio.create_task(consume())
        assert await asyncio.to_thread(tool.started.wait, 5)
        assert loop.request_cancel(session)
        assert loop.request_cancel(session)
        tool.release.set()
        await owner

    asyncio.run(run())
    assert tool.calls == 1
    assert tool.path.read_text(encoding="utf-8") == "written"
    assert [r[6] for r in rows(store, "tool_executions", session.id)] == [
        "succeeded",
        "skipped",
    ]
    assert [r[2] for r in rows(store, "turns", session.id)] == ["cancelled"]
    assert isinstance(events[-1], TurnCancelled)
    session.finish()
    restored = Session.resume(
        session.id, store=store, workspace=str(tmp_path), model="model", base_url="url"
    )
    assert (
        asyncio.run(make_loop(Provider([answer()])).run(restored, "continue")) == "done"
    )
    store.close()


def test_external_cancel_during_model_drops_delta_and_session_can_finish(tmp_path):
    gate = asyncio.Event()
    entered = asyncio.Event()

    class Partial(LLMProvider):
        closed = False

        async def stream(self, request):
            try:
                yield LLMTextDelta(delta="not committed")
                entered.set()
                await gate.wait()
            finally:
                self.closed = True

    store = SQLiteSessionStore(tmp_path / "history.sqlite3")
    session = Session(
        store=store, workspace=str(tmp_path), model="model", base_url="url"
    )
    provider = Partial()
    events = []

    async def consume():
        async for event in make_loop(provider).stream(session, "first"):
            events.append(event)

    async def run():
        owner = asyncio.create_task(consume())
        await entered.wait()
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner

    asyncio.run(run())
    assert provider.closed
    assert any(isinstance(e, AssistantTextDelta) for e in events)
    assert len(rows(store, "messages", session.id)) == 2
    assert [r[2] for r in rows(store, "turns", session.id)] == ["cancelled"]
    session.finish()
    store.close()


def test_cancel_before_first_model_event_leaves_only_user_input(tmp_path):
    entered = asyncio.Event()

    class Waiting(LLMProvider):
        async def stream(self, request):
            entered.set()
            await asyncio.Future()
            yield LLMResponseCompleted(response=answer())

    store = SQLiteSessionStore(tmp_path / "history.sqlite3")
    session = Session(store=store)

    async def run():
        owner = asyncio.create_task(make_loop(Waiting()).run(session, "hello"))
        await entered.wait()
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner

    asyncio.run(run())
    assert len(rows(store, "messages", session.id)) == 2
    assert [r[2] for r in rows(store, "turns", session.id)] == ["cancelled"]
    session.finish()
    store.close()


def test_cancel_second_tool_keeps_first_result_and_skips_third(tmp_path):
    class SequenceTool(BarrierTool):
        def execute(self, arguments):
            self.calls += 1
            if self.calls == 1:
                return "first result"
            self.started.set()
            assert self.release.wait(timeout=5)
            return "second result"

    store = SQLiteSessionStore(tmp_path / "history.sqlite3")
    session = Session(store=store)
    tool = SequenceTool(tmp_path / "unused.txt")
    loop = make_loop(Provider([tools_response("one", "two", "three")]), tool)

    async def consume():
        async for _ in loop.stream(session, "first"):
            pass

    async def run():
        owner = asyncio.create_task(consume())
        assert await asyncio.to_thread(tool.started.wait, 5)
        assert loop.request_cancel(session)
        tool.release.set()
        await owner

    asyncio.run(run())
    assert tool.calls == 2
    assert [r[6] for r in rows(store, "tool_executions", session.id)] == [
        "succeeded",
        "succeeded",
        "skipped",
    ]
    assert [m.content for m in session.messages if isinstance(m, ToolMessage)] == [
        "first result",
        "second result",
        "Skipped: turn stopped before execution; tool did not run",
    ]
    session.finish()
    store.close()


def test_storage_failure_keeps_turn_unsettled_and_does_not_emit_completion(tmp_path):
    store = SQLiteSessionStore(tmp_path / "history.sqlite3")
    session = Session(store=store, workspace=str(tmp_path), model="model")
    with store._transaction() as connection:
        connection.execute("""CREATE TRIGGER reject_final BEFORE UPDATE OF status ON turns
            WHEN NEW.status='completed' BEGIN SELECT RAISE(ABORT, 'cannot commit final'); END""")
    events = []

    async def run():
        async for event in make_loop(Provider([answer()])).stream(session, "hello"):
            events.append(event)

    with pytest.raises(StorageError, match="cannot commit final"):
        asyncio.run(run())
    assert not any(isinstance(e, TurnCompleted) for e in events)
    with pytest.raises((StorageError, ResumeError)):
        session.finish()
    with sqlite3.connect(tmp_path / "history.sqlite3") as reader:
        assert reader.execute("SELECT status FROM turns").fetchone()[0] == "running"
        assert reader.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    store.close()


def test_failed_turn_and_concurrent_turn_guard(tmp_path):
    store = SQLiteSessionStore(tmp_path / "history.sqlite3")
    session = Session(
        store=store, workspace=str(tmp_path), model="model", base_url="url"
    )
    turn = session.begin_turn("first", "system")
    with pytest.raises(ResumeError, match="unsettled"):
        session.begin_turn("second", "system")
    with pytest.raises(ResumeError, match="unsettled"):
        session.finish()
    session.end_turn(turn, "failed", "model failed")
    assert asyncio.run(make_loop(Provider([answer()])).run(session, "next")) == "done"
    assert [r[2] for r in rows(store, "turns", session.id)] == ["failed", "completed"]
    store.close()


def test_final_answer_commit_wins_late_cancel(tmp_path):
    store = SQLiteSessionStore(tmp_path / "history.sqlite3")
    session = Session(store=store)
    loop = make_loop(Provider([answer()]))
    events = []

    async def run():
        async for event in loop.stream(session, "hello"):
            events.append(event)
            if isinstance(event, ModelCompleted):
                assert loop.request_cancel(session)

    asyncio.run(run())
    assert isinstance(events[-1], TurnCompleted)
    assert [r[2] for r in rows(store, "turns", session.id)] == ["completed"]
    store.close()


def test_repeated_cli_sigint_settles_and_prompt_exit_closes(tmp_path):
    class Interrupting(LLMProvider):
        async def stream(self, request):
            signal.raise_signal(signal.SIGINT)
            signal.raise_signal(signal.SIGINT)
            await asyncio.Future()
            yield LLMResponseCompleted(response=answer())

    store = SQLiteSessionStore(tmp_path / "history.sqlite3")
    session = Session(store=store)
    screen = StringIO()
    inputs = iter(["hello", "exit"])
    result = asyncio.run(
        run_repl(
            make_loop(Interrupting()),
            session=session,
            console=Console(file=screen),
            read_input=lambda _: next(inputs),
        )
    )
    assert result == 0
    assert screen.getvalue().count("正在停止") == 2
    assert "当前回合已取消，收尾完成" in screen.getvalue()
    assert [r[2] for r in rows(store, "turns", session.id)] == ["cancelled"]
    assert (
        store.list_sessions(str(tmp_path)) == ()
    )  # Session has no workspace metadata.
    with store._transaction() as connection:
        assert (
            connection.execute(
                "SELECT status FROM sessions WHERE id=?", (session.id,)
            ).fetchone()[0]
            == "closed"
        )
    store.close()


def test_outer_repl_cancellation_finishes_settled_session(tmp_path):
    entered = asyncio.Event()

    class Waiting(LLMProvider):
        async def stream(self, request):
            entered.set()
            await asyncio.Future()
            yield LLMResponseCompleted(response=answer())

    store = SQLiteSessionStore(tmp_path / "history.sqlite3")
    session = Session(
        store=store, workspace=str(tmp_path), model="model", base_url="url"
    )
    reader = iter(["hello"])

    async def run():
        repl = asyncio.create_task(
            run_repl(
                make_loop(Waiting()),
                session=session,
                console=Console(file=StringIO()),
                read_input=lambda _: next(reader),
            )
        )
        await entered.wait()
        repl.cancel()
        with pytest.raises(asyncio.CancelledError):
            await repl

    asyncio.run(run())
    assert [r[2] for r in rows(store, "turns", session.id)] == ["cancelled"]
    resumed = Session.resume(
        session.id, store=store, workspace=str(tmp_path), model="model", base_url="url"
    )
    assert resumed.id == session.id
    store.close()


def test_duplicate_tool_ids_fail_before_assistant_commit(tmp_path):
    store = SQLiteSessionStore(tmp_path / "history.sqlite3")
    session = Session(store=store)
    loop = make_loop(Provider([tools_response("duplicate", "duplicate")]))
    events = []

    async def run():
        async for event in loop.stream(session, "hello"):
            events.append(event)

    with pytest.raises(Exception, match="Duplicate tool call IDs"):
        asyncio.run(run())
    assert isinstance(events[-1], TurnFailed)
    assert [r[2] for r in rows(store, "turns", session.id)] == ["failed"]
    assert len(rows(store, "messages", session.id)) == 2
    store.close()


def test_begin_turn_write_failure_rolls_back_system_and_user(tmp_path):
    store = SQLiteSessionStore(tmp_path / "history.sqlite3")
    session = Session(store=store)
    with store._transaction() as connection:
        connection.execute("""CREATE TRIGGER reject_user BEFORE INSERT ON messages
            WHEN NEW.sequence=2 BEGIN SELECT RAISE(ABORT, 'cannot save user'); END""")
    with pytest.raises(StorageError, match="cannot save user"):
        session.begin_turn("hello", "system")
    assert session.records == ()
    assert session.turn is None
    with sqlite3.connect(tmp_path / "history.sqlite3") as reader:
        assert reader.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
        assert reader.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    store.close()


def test_tool_result_commit_failure_does_not_emit_completion(tmp_path):
    store = SQLiteSessionStore(tmp_path / "history.sqlite3")
    session = Session(store=store)
    tool = BarrierTool(tmp_path / "effect.txt")
    tool.release.set()
    with store._transaction() as connection:
        connection.execute("""CREATE TRIGGER reject_result BEFORE INSERT ON messages
            WHEN NEW.sequence=4 BEGIN SELECT RAISE(ABORT, 'cannot save result'); END""")
    events = []

    async def run():
        async for event in make_loop(Provider([tools_response("one")]), tool).stream(
            session, "hello"
        ):
            events.append(event)

    with pytest.raises(StorageError, match="cannot save result"):
        asyncio.run(run())
    assert tool.path.read_text(encoding="utf-8") == "written"
    assert not any(event.type == "tool.completed" for event in events)
    assert session.turn.tools[0].result is None
    with sqlite3.connect(tmp_path / "history.sqlite3") as reader:
        assert reader.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3
        assert (
            reader.execute("SELECT status FROM tool_executions").fetchone()[0]
            == "running"
        )
    store.close()
