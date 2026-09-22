from io import StringIO

from rich.console import Console

from codehead.cli import run_repl
from codehead.errors import ModelError
from codehead.session import Session


class FakeLoop:
    def __init__(self) -> None:
        self.calls: list[tuple[Session, str]] = []

    def run(self, session: Session, user_input: str) -> str:
        self.calls.append((session, user_input))
        return f"answer: {user_input}"


class FailingLoop:
    def run(self, session: Session, user_input: str) -> str:
        raise ModelError("unavailable")


def test_repl_keeps_one_session_and_handles_exit() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    inputs = iter(["", "first", "second", "quit"])
    loop = FakeLoop()

    exit_code = run_repl(
        loop,  # type: ignore[arg-type]
        console=console,
        read_input=lambda prompt: next(inputs),
    )

    assert exit_code == 0
    assert [user_input for _, user_input in loop.calls] == ["first", "second"]
    assert loop.calls[0][0] is loop.calls[1][0]
    assert "answer: first" in output.getvalue()
    assert "answer: second" in output.getvalue()


def test_repl_returns_nonzero_for_model_error() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)

    exit_code = run_repl(
        FailingLoop(),  # type: ignore[arg-type]
        console=console,
        read_input=lambda prompt: "hello",
    )

    assert exit_code == 1
    assert "Error: unavailable" in output.getvalue()


def test_repl_treats_eof_as_normal_exit() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)

    def end_input(prompt: str) -> str:
        raise EOFError

    assert run_repl(
        FakeLoop(),  # type: ignore[arg-type]
        console=console,
        read_input=end_input,
    ) == 0
