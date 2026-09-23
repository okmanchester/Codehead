import asyncio
from io import StringIO
from unittest.mock import AsyncMock, Mock, patch

import pytest
from rich.console import Console

from codehead.cli import _main, build_agent_loop, main, run_repl
from codehead.config import Config
from codehead.errors import ContextError, ModelError, StorageError
from codehead.models import (
    AssistantMessage,
    AssistantTextDelta,
    ContextCompacted,
    ContextCompactionStarted,
    FinishReason,
    LLMResponse,
    ModelCompleted,
    ToolCall,
    ToolCompleted,
    ToolResult,
    ToolStarted,
    TurnCompleted,
    UserMessage,
)
from codehead.session import Session, SQLiteSessionStore


class FakeLoop:
    def __init__(self) -> None:
        self.calls: list[tuple[Session, str]] = []

    async def stream(self, session: Session, user_input: str):
        self.calls.append((session, user_input))
        answer = f"answer: {user_input}"
        yield AssistantTextDelta(step=1, delta=answer)
        yield ModelCompleted(
            step=1,
            response=LLMResponse(
                message=AssistantMessage(content=answer),
                finish_reason=FinishReason.END_TURN,
            ),
        )
        yield TurnCompleted(step=1, answer=answer)


class FailingLoop:
    async def stream(self, session: Session, user_input: str):
        raise ModelError("unavailable")
        yield


def test_repl_keeps_one_session_and_handles_exit() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    inputs = iter(["", "first", "second", "quit"])
    loop = FakeLoop()

    exit_code = asyncio.run(
        run_repl(
            loop,  # type: ignore[arg-type]
            console=console,
            read_input=lambda prompt: next(inputs),
        )
    )

    assert exit_code == 0
    assert [user_input for _, user_input in loop.calls] == ["first", "second"]
    assert loop.calls[0][0] is loop.calls[1][0]
    assert output.getvalue().count("answer: first") == 1
    assert output.getvalue().count("answer: second") == 1


def test_repl_continues_after_model_error() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    inputs = iter(["hello", "quit"])

    exit_code = asyncio.run(
        run_repl(
            FailingLoop(),  # type: ignore[arg-type]
            console=console,
            read_input=lambda prompt: next(inputs),
        )
    )

    assert exit_code == 0
    assert "Error: unavailable" in output.getvalue()


def test_repl_reuses_session_with_failed_turn_history() -> None:
    output = StringIO()

    class RecoveringLoop:
        session: Session | None = None

        async def stream(self, session: Session, user_input: str):
            if self.session is None:
                self.session = session
                session.add(UserMessage(content=user_input))
                raise ModelError("temporary failure")
            assert session is self.session
            assert [message.content for message in session.messages] == ["first"]
            session.add(UserMessage(content=user_input))
            yield AssistantTextDelta(step=1, delta="recovered")
            yield ModelCompleted(
                step=1,
                response=LLMResponse(
                    message=AssistantMessage(content="recovered"),
                    finish_reason=FinishReason.END_TURN,
                ),
            )
            yield TurnCompleted(step=1, answer="recovered")

    inputs = iter(["first", "second", "quit"])
    loop = RecoveringLoop()
    result = asyncio.run(
        run_repl(
            loop,  # type: ignore[arg-type]
            console=Console(file=output, force_terminal=False),
            read_input=lambda _: next(inputs),
        )
    )
    assert result == 0
    assert "Error: temporary failure" in output.getvalue()
    assert "recovered" in output.getvalue()
    assert [message.content for message in loop.session.messages] == ["first", "second"]


def test_repl_treats_eof_as_normal_exit() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)

    def end_input(prompt: str) -> str:
        raise EOFError

    assert (
        asyncio.run(
            run_repl(
                FakeLoop(),  # type: ignore[arg-type]
                console=console,
                read_input=end_input,
            )
        )
        == 0
    )


def test_repl_renders_deltas_immediately_and_tool_status_without_output() -> None:
    output = StringIO()

    class StreamingLoop(FakeLoop):
        async def stream(self, session, user_input):
            yield AssistantTextDelta(step=1, delta="[literal]")
            assert output.getvalue().endswith("Codehead> [literal]")
            yield AssistantTextDelta(step=1, delta=" text")
            yield ModelCompleted(
                step=1,
                response=LLMResponse(
                    message=AssistantMessage(content="[literal] text"),
                    finish_reason=FinishReason.TOOL_CALLS,
                ),
            )
            for call_id, is_error in (("one", False), ("two", True)):
                yield ToolStarted(step=1, tool_call=ToolCall(id=call_id, name="read"))
                yield ToolCompleted(
                    step=1,
                    result=ToolResult(
                        tool_call_id=call_id,
                        output="private tool output",
                        is_error=is_error,
                    ),
                )
            async for event in super().stream(session, user_input):
                yield event

    inputs = iter(["hello", "exit"])
    assert (
        asyncio.run(
            run_repl(
                StreamingLoop(),
                console=Console(file=output, force_terminal=False),
                read_input=lambda _: next(inputs),
            )
        )
        == 0
    )
    rendered = output.getvalue()
    assert rendered.count("[literal] text") == 1
    assert rendered.count("answer: hello") == 1
    assert "read: started" in rendered
    assert "read: succeeded" in rendered
    assert "read: failed" in rendered
    assert "private tool output" not in rendered


@pytest.mark.parametrize(
    "failure", [ModelError("broken"), asyncio.CancelledError(), KeyboardInterrupt()]
)
def test_partial_output_failure_closes_generator(failure) -> None:
    output = StringIO()

    class InterruptedLoop:
        closed = False

        async def stream(self, session, user_input):
            try:
                yield AssistantTextDelta(step=1, delta="partial")
                raise failure
            finally:
                self.closed = True

    loop = InterruptedLoop()
    inputs = iter(["hello", "quit"])
    coroutine = run_repl(
        loop, console=Console(file=output), read_input=lambda _: next(inputs)
    )
    assert asyncio.run(coroutine) == 0
    assert loop.closed
    assert "partial\n" in output.getvalue()
    if isinstance(failure, ModelError):
        assert "partial\nError: broken" in output.getvalue()


@pytest.mark.parametrize(
    "failure", [None, ModelError("broken"), asyncio.CancelledError()]
)
def test_main_closes_loop_on_exit_and_failure(failure, tmp_path) -> None:
    loop = Mock(aclose=AsyncMock())
    repl = AsyncMock(return_value=0, side_effect=failure)
    store = SQLiteSessionStore(tmp_path / "archive.sqlite3")
    with (
        patch(
            "codehead.cli.Config.from_env",
            return_value=Config(api_key="test", workspace=tmp_path),
        ),
        patch("codehead.cli.build_agent_loop", return_value=loop),
        patch("codehead.cli.SQLiteSessionStore", return_value=store),
        patch("codehead.cli.run_repl", repl),
    ):
        if failure is None:
            assert asyncio.run(_main()) == 0
        else:
            with pytest.raises(type(failure)):
                asyncio.run(_main())
    loop.aclose.assert_awaited_once()
    assert store._connection is None


def test_main_treats_keyboard_interrupt_as_normal_exit() -> None:
    with patch("codehead.cli._main", side_effect=KeyboardInterrupt):
        assert main() == 0


def test_startup_requires_ripgrep(tmp_path) -> None:
    config = Config(api_key="test", workspace=tmp_path)
    with (
        patch(
            "codehead.cli.find_ripgrep",
            side_effect=ValueError("ripgrep (rg) is required"),
        ),
        pytest.raises(ValueError, match="ripgrep"),
    ):
        build_agent_loop(config)


@pytest.mark.parametrize("fatal", [False, True])
def test_context_error_continues_but_storage_error_stops_repl(fatal):
    output = StringIO()

    class ErrorLoop(FakeLoop):
        async def stream(self, session, user_input):
            if user_input == "first":
                if fatal:
                    raise StorageError("archive failed")
                raise ContextError("summary failed")
            async for event in super().stream(session, user_input):
                yield event

    loop = ErrorLoop()
    inputs = iter(["first", "second", "quit"])
    result = asyncio.run(
        run_repl(loop, console=Console(file=output), read_input=lambda _: next(inputs))
    )
    assert result == int(fatal)
    assert len(loop.calls) == (0 if fatal else 1)
    assert ("archive failed" if fatal else "summary failed") in output.getvalue()


def test_repl_displays_context_status_and_estimates():
    output = StringIO()

    class CompactingLoop(FakeLoop):
        async def stream(self, session, user_input):
            yield ContextCompactionStarted(step=1, before_tokens=3000)
            yield ContextCompacted(step=1, before_tokens=3000, after_tokens=1000)
            async for event in super().stream(session, user_input):
                yield event

    inputs = iter(["hello", "quit"])
    assert (
        asyncio.run(
            run_repl(
                CompactingLoop(),
                console=Console(file=output, width=120),
                read_input=lambda _: next(inputs),
            )
        )
        == 0
    )
    assert "Context> compacting (~3000 input tokens)" in output.getvalue()
    assert "Context> compacted: ~3000 -> ~1000 input tokens" in output.getvalue()


def test_database_startup_error_closes_provider(tmp_path):
    loop = Mock(aclose=AsyncMock())
    with (
        patch(
            "codehead.cli.Config.from_env",
            return_value=Config(api_key="test", workspace=tmp_path),
        ),
        patch("codehead.cli.build_agent_loop", return_value=loop),
        patch(
            "codehead.cli.SQLiteSessionStore", side_effect=StorageError("cannot open")
        ),
    ):
        assert asyncio.run(_main()) == 1
    loop.aclose.assert_awaited_once()
