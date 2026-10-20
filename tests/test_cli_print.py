"""Single-turn (--print) CLI mode: stdout contract, exit codes, and UTF-8 streams."""

import asyncio
import sys
from io import StringIO
from unittest.mock import patch

import pytest
from rich.console import Console

from codehead.cli import _configure_utf8_stdio, _main, _parse_args, build_agent_loop
from codehead.config import RuntimeConfig
from codehead.errors import ModelError
from codehead.models import (
    AssistantMessage,
    FinishReason,
    LLMResponse,
    ModelCompleted,
    ModelStarted,
    TokenUsage,
    ToolCall,
    TurnCompleted,
)


class FakeLoop:
    """Yield one completed turn, or fail before any event."""

    def __init__(
        self,
        answer: str = "done",
        usage: TokenUsage | None = None,
        failure: BaseException | None = None,
        reasoning_content: str | None = None,
    ) -> None:
        self._answer = answer
        self._usage = usage
        self._failure = failure
        self._reasoning_content = reasoning_content
        self.closed = False

    async def stream(self, session, user_input):
        if self._failure is not None:
            raise self._failure
        yield ModelStarted(step=1)
        yield ModelCompleted(
            step=1,
            response=LLMResponse(
                message=AssistantMessage(
                    content=self._answer, reasoning_content=self._reasoning_content
                ),
                finish_reason=FinishReason.END_TURN,
                usage=self._usage,
            ),
        )
        yield TurnCompleted(step=1, answer=self._answer)

    async def aclose(self) -> None:
        self.closed = True


def run_print_main(loop: FakeLoop, argv: list[str], tmp_path, *, thinking="disabled"):
    out, err = StringIO(), StringIO()
    config = RuntimeConfig(api_key="test", workspace=tmp_path, thinking=thinking)
    with (
        patch("codehead.cli.load_runtime_config", return_value=config),
        patch("codehead.cli.build_agent_loop", return_value=loop),
        patch("codehead.cli.Console", return_value=Console(file=err, width=200)),
        patch.object(sys, "stdout", out),
    ):
        code = asyncio.run(_main(argv))
    return code, out.getvalue(), err.getvalue()


def test_print_thinking_keeps_reasoning_and_wait_status_out_of_output(tmp_path):
    loop = FakeLoop(answer="answer", reasoning_content="private thought")
    args = ["-p", "question"]
    code, out, err = run_print_main(loop, args, tmp_path, thinking="enabled")
    assert code == 0
    assert "answer" in out
    assert "private thought" not in out + err
    assert "等待模型响应" not in out + err


def test_print_writes_only_the_answer_to_stdout(tmp_path) -> None:
    loop = FakeLoop(
        answer="the answer",
        usage=TokenUsage(input_tokens=10, output_tokens=4, total_tokens=14),
    )
    code, out, err = run_print_main(loop, ["-p", "hi"], tmp_path)
    assert code == 0
    assert out == "the answer\n"
    assert loop.closed
    # Session banner and usage stay on stderr so stdout carries the answer alone.
    assert "New session:" in err
    assert "usage input=10 output=4 total=14" in err


def test_print_reads_the_prompt_from_stdin_when_value_is_omitted(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(sys, "stdin", StringIO("from stdin\n"))
    code, out, _ = run_print_main(FakeLoop(answer="ok"), ["-p"], tmp_path)
    assert code == 0
    assert out == "ok\n"


def test_print_rejects_an_empty_prompt(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", StringIO("   \n"))
    code, out, err = run_print_main(FakeLoop(), ["-p"], tmp_path)
    assert code == 1
    assert out == ""
    assert "empty prompt" in err


def test_print_reports_model_errors_and_returns_one(tmp_path) -> None:
    loop = FakeLoop(failure=ModelError("upstream down"))
    code, out, err = run_print_main(loop, ["-p", "hi"], tmp_path)
    assert code == 1
    assert out == ""
    assert "upstream down" in err


def test_print_treats_keyboard_interrupt_as_failure(tmp_path) -> None:
    loop = FakeLoop(failure=KeyboardInterrupt())
    code, out, err = run_print_main(loop, ["-p", "hi"], tmp_path)
    assert code == 1
    assert out == ""
    assert "Interrupted" in err


def test_allow_all_tools_requires_print() -> None:
    with pytest.raises(SystemExit) as exit_info:
        _parse_args(["--allow-all-tools"])
    assert exit_info.value.code == 2


def test_build_agent_loop_unattended_policy_grants_write_tools(tmp_path) -> None:
    config = RuntimeConfig(api_key="test", workspace=tmp_path)
    with (
        patch("codehead.cli.find_ripgrep", return_value="rg"),
        patch("codehead.cli.OpenAIProvider"),
    ):
        loop = build_agent_loop(config, allow_all_tools=True)
    call = ToolCall(id="1", name="write", arguments={"path": "a.txt", "content": "x"})
    assert loop._permissions.action(call) == "allow"


def test_build_agent_loop_default_policy_still_asks_for_writes(tmp_path) -> None:
    config = RuntimeConfig(api_key="test", workspace=tmp_path)
    with (
        patch("codehead.cli.find_ripgrep", return_value="rg"),
        patch("codehead.cli.OpenAIProvider"),
    ):
        loop = build_agent_loop(config)
    call = ToolCall(id="1", name="write", arguments={"path": "a.txt", "content": "x"})
    assert loop._permissions.action(call) == "ask"


class FakeStream:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def reconfigure(self, **kwargs) -> None:
        self.calls.append(kwargs)


def test_configure_utf8_stdio_binds_every_stream(monkeypatch) -> None:
    streams = [FakeStream(), FakeStream(), FakeStream()]
    monkeypatch.setattr(sys, "stdin", streams[0])
    monkeypatch.setattr(sys, "stdout", streams[1])
    monkeypatch.setattr(sys, "stderr", streams[2])
    _configure_utf8_stdio()
    for stream in streams:
        assert stream.calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_configure_utf8_stdio_tolerates_streams_without_reconfigure(
    monkeypatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", None)
    monkeypatch.setattr(sys, "stdout", StringIO())
    monkeypatch.setattr(sys, "stderr", StringIO())
    _configure_utf8_stdio()  # skipped, not fatal, for streams that cannot reconfigure
