"""Command-line entry point for Codehead."""

import argparse
import asyncio
import json
import signal
import sys
import threading
from collections.abc import Callable
from contextlib import aclosing
from typing import TextIO

from rich.console import Console
from rich.text import Text

from codehead.agent import Agent, AgentLoop
from codehead.cancellation import settle
from codehead.config import RuntimeConfig
from codehead.config_loader import load_local_config, load_runtime_config
from codehead.errors import (
    AgentAbortedError,
    CodeheadError,
    ResumeError,
    StorageError,
    ToolCleanupError,
)
from codehead.llm import OpenAIProvider
from codehead.models import (
    AssistantTextDelta,
    ContextCompacted,
    ContextCompactionStarted,
    ModelCompleted,
    SystemMessage,
    TokenUsage,
    ToolApprovalRequested,
    ToolCompleted,
    ToolStarted,
    TurnCancelled,
    TurnCompleted,
)
from codehead.permissions import ApprovalChoice, PermissionPolicy
from codehead.prompt import build_system_prompt
from codehead.session import Session, SQLiteSessionStore
from codehead.tools import (
    BashTool,
    EditTool,
    GlobTool,
    GrepTool,
    ReadTool,
    ToolRegistry,
    WriteTool,
)
from codehead.tools.search import find_ripgrep


def build_agent_loop(
    config: RuntimeConfig, *, allow_all_tools: bool = False
) -> AgentLoop:
    """Construct the v0.1 agent and its built-in tools."""
    rg_executable = find_ripgrep()
    tools = ToolRegistry(
        [
            ReadTool(config.workspace),
            GlobTool(config.workspace, rg_executable),
            GrepTool(config.workspace, rg_executable),
            WriteTool(config.workspace),
            EditTool(config.workspace),
            BashTool(config.workspace),
        ]
    )
    names = (definition.name for definition in tools.definitions())
    policy = (
        PermissionPolicy.allow_all(config.workspace, names)
        if allow_all_tools
        else PermissionPolicy.from_file(config.workspace, names)
    )
    provider = OpenAIProvider(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        disable_thinking=True,
    )
    agent = Agent(
        provider=provider,
        tools=tools,
        system_prompt=build_system_prompt(config.workspace),
    )
    return AgentLoop(
        agent,
        max_steps=config.max_steps,
        context_budget=config.context_budget,
        permission_policy=policy,
    )


async def run_repl(
    loop: AgentLoop,
    *,
    console: Console,
    read_input: Callable[[str], str] | None = None,
    session: Session | None = None,
) -> int:
    """Run a session; production callers inject a disk-backed session."""
    active_session = session if session is not None else Session()
    try:
        return await _run_repl(loop, active_session, console, read_input)
    finally:
        if session is None:
            active_session.close()


async def _run_repl(
    loop: AgentLoop,
    session: Session,
    console: Console,
    read_input: Callable[[str], str] | None,
) -> int:
    reader = read_input or console.input
    console.print("Codehead is ready. Type 'exit' or 'quit' to leave.")
    current: asyncio.Task[None] | None = None
    stopping = False
    approval_input = False

    async def approve(call) -> ApprovalChoice:
        nonlocal approval_input
        console.print(Text(f"Permission> {call.name} requested with arguments:"))
        console.print(Text(json.dumps(call.arguments, ensure_ascii=False, indent=2)))
        while True:
            approval_input = True
            try:
                answer = (
                    reader(
                        "[bold yellow]Allow once [y], this session [s], deny [n; default]?[/bold yellow] "
                    )
                    .strip()
                    .lower()
                )
            except EOFError as exc:
                raise asyncio.CancelledError from exc
            finally:
                approval_input = False
            if answer in ("y", "yes"):
                return "once"
            if answer in ("s", "session"):
                return "session"
            if answer in ("", "n", "no"):
                return "deny"
            console.print(Text("Enter y, s, or n."))

    if isinstance(loop, AgentLoop):
        loop.set_approval_callback(approve)

    def interrupt(signum: int, frame: object) -> None:
        nonlocal stopping
        if current is None:
            signal.default_int_handler(signum, frame)
        else:
            console.print(Text(loop.cancellation_status(session)))
            if not stopping:
                stopping = True
                if not loop.request_cancel(session):
                    current.cancel()
            if approval_input:
                raise KeyboardInterrupt

    previous = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGINT, interrupt)
    try:
        while True:
            try:
                user_input = reader("[bold green]You>[/bold green] ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                return _finish_session(session, console)
            if not user_input:
                continue
            if user_input.lower() in {"exit", "quit"}:
                return _finish_session(session, console)
            stopping = False
            current = asyncio.create_task(
                _consume_turn(loop, session, user_input, console)
            )
            try:
                await asyncio.shield(current)
            except asyncio.CancelledError:
                # A cancelled child is a single-turn interruption. Cancellation of
                # this REPL task additionally releases the session before escaping.
                outer_cancelled = asyncio.current_task().cancelling() > 0
                if not current.done() and not stopping:
                    stopping = True
                    console.print(Text(loop.cancellation_status(session)))
                    current.cancel()
                try:
                    await settle(current)
                except asyncio.CancelledError:
                    pass
                except (StorageError, ToolCleanupError) as exc:
                    console.print(
                        Text(
                            f"Error: {exc}; cleanup incomplete, session cannot be resumed",
                            style="bold red",
                        )
                    )
                    if outer_cancelled:
                        raise asyncio.CancelledError from exc
                    return 1
                if outer_cancelled:
                    _finish_session(session, console)
                    raise
                console.print("当前回合已取消，收尾完成。")
            except (StorageError, ToolCleanupError) as exc:
                console.print(
                    Text(
                        f"Error: {exc}; cleanup incomplete, session stopped",
                        style="bold red",
                    )
                )
                return 1
            except CodeheadError as exc:
                console.print(Text(f"Error: {exc}", style="bold red"))
            finally:
                current = None
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
        if isinstance(loop, AgentLoop):
            loop.set_approval_callback(None)


async def _consume_turn(
    loop: AgentLoop, session: Session, user_input: str, console: Console
) -> None:
    line_open = False
    tool_names: dict[str, str] = {}
    try:
        async with aclosing(loop.stream(session, user_input)) as events:
            async for event in events:
                if isinstance(event, AssistantTextDelta):
                    if not line_open:
                        console.print(Text("Codehead> ", style="bold cyan"), end="")
                        line_open = True
                    console.print(Text(event.delta), end="", soft_wrap=True)
                elif isinstance(event, ModelCompleted):
                    if line_open:
                        console.print()
                        line_open = False
                elif isinstance(event, ContextCompactionStarted):
                    console.print(
                        Text(
                            f"Context> compacting (~{event.before_tokens} input tokens)"
                        )
                    )
                elif isinstance(event, ContextCompacted):
                    console.print(
                        Text(
                            f"Context> compacted: ~{event.before_tokens} -> ~{event.after_tokens} input tokens"
                        )
                    )
                elif isinstance(event, TurnCancelled):
                    console.print("当前回合已取消，收尾完成。")
                elif isinstance(event, ToolStarted):
                    tool_names[event.execution_id or event.tool_call.id] = (
                        event.tool_call.name
                    )
                    console.print(Text(f"Tool> {event.tool_call.name}: started"))
                elif isinstance(event, ToolApprovalRequested):
                    tool_names[event.execution_id] = event.tool_call.name
                elif isinstance(event, ToolCompleted):
                    name = tool_names.pop(
                        event.execution_id or event.result.tool_call_id,
                        event.tool_name or event.result.tool_call_id,
                    )
                    console.print(Text(f"Tool> {name}: {event.result.outcome}"))
    except KeyboardInterrupt as exc:
        # Do not allow KeyboardInterrupt to escape an asyncio child Task.
        raise asyncio.CancelledError from exc
    finally:
        if line_open:
            console.print()


def _finish_session(session: Session, console: Console) -> int:
    try:
        session.finish()
    except (ResumeError, StorageError) as exc:
        console.print(
            Text(f"Error: {exc}; session cannot be resumed", style="bold red")
        )
        return 1
    return 0


def _add_usage(total: TokenUsage | None, usage: TokenUsage | None) -> TokenUsage | None:
    if usage is None:
        return total
    if total is None:
        return usage
    return TokenUsage(
        input_tokens=total.input_tokens + usage.input_tokens,
        output_tokens=total.output_tokens + usage.output_tokens,
        total_tokens=total.total_tokens + usage.total_tokens,
    )


async def _consume_print_turn(
    loop: AgentLoop, session: Session, user_input: str, console: Console
) -> tuple[str, TokenUsage | None]:
    """Run one turn without rendering deltas; return its answer and total usage."""
    answer: str | None = None
    usage: TokenUsage | None = None
    async with aclosing(loop.stream(session, user_input)) as events:
        async for event in events:
            if isinstance(event, ModelCompleted):
                usage = _add_usage(usage, event.response.usage)
            elif isinstance(event, TurnCompleted):
                answer = event.answer
            elif isinstance(event, ContextCompactionStarted):
                console.print(
                    Text(f"Context> compacting (~{event.before_tokens} input tokens)")
                )
            elif isinstance(event, ContextCompacted):
                console.print(
                    Text(
                        f"Context> compacted: ~{event.before_tokens} -> "
                        f"~{event.after_tokens} input tokens"
                    )
                )
            elif isinstance(event, ToolStarted):
                console.print(Text(f"Tool> {event.tool_call.name}: started"))
            elif isinstance(event, ToolApprovalRequested):
                console.print(Text(f"Tool> {event.tool_call.name}: approval required"))
            elif isinstance(event, ToolCompleted):
                name = event.tool_name or event.result.tool_call_id
                console.print(Text(f"Tool> {name}: {event.result.outcome}"))
    if answer is None:
        raise AgentAbortedError("Agent stream ended without a final answer")
    return answer, usage


async def run_print(
    loop: AgentLoop,
    session: Session,
    prompt: str,
    *,
    console: Console,
    out: TextIO | None = None,
) -> int:
    """Run one turn without the REPL; write only the final answer to stdout."""
    writer = out if out is not None else sys.stdout
    try:
        answer, usage = await _consume_print_turn(loop, session, prompt, console)
    except KeyboardInterrupt:
        console.print(Text("Interrupted", style="bold red"))
        return 1
    except CodeheadError as exc:
        console.print(Text(f"Error: {exc}", style="bold red"))
        return 1
    writer.write(answer)
    writer.write("\n")
    writer.flush()
    if usage is not None:
        console.print(
            Text(
                f"[codehead] usage input={usage.input_tokens} "
                f"output={usage.output_tokens} total={usage.total_tokens}"
            )
        )
    return _finish_session(session, console)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="codehead")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--list-sessions",
        action="store_true",
        help="List sessions in the current workspace",
    )
    modes.add_argument(
        "--resume", metavar="ID", help="Resume a normally closed session by its full ID"
    )
    modes.add_argument(
        "-p",
        "--print",
        dest="print_prompt",
        nargs="?",
        const="",
        metavar="PROMPT",
        help="Run one turn without the REPL; read the prompt from stdin when omitted",
    )
    parser.add_argument(
        "--allow-all-tools",
        action="store_true",
        help="Grant every tool without prompting; requires --print",
    )
    args = parser.parse_args(argv)
    if args.allow_all_tools and args.print_prompt is None:
        parser.error("--allow-all-tools requires --print")
    return args


def _list_sessions(console: Console) -> int:
    try:
        config = load_local_config()
        if not config.session_db_path.exists():
            console.print("No sessions in this workspace.")
            return 0
        store = SQLiteSessionStore(config.session_db_path)
        try:
            sessions = store.list_sessions(str(config.workspace))
        finally:
            store.close()
        if not sessions:
            console.print("No sessions in this workspace.")
        for info in sessions:
            status = {
                "legacy": "legacy (exit status unknown)",
                "active": "active (running or not closed normally)",
                "closed": "closed",
            }.get(info.status, info.status)
            console.print(
                Text(
                    f"{info.id}  {info.updated_at} UTC  {info.model or '-'}  {status}"
                ),
                soft_wrap=True,
            )
    except (CodeheadError, ValueError) as exc:
        console.print(Text(f"Error: {exc}", style="bold red"))
        return 1
    return 0


def _open_session(
    config: RuntimeConfig,
    store: SQLiteSessionStore,
    resume_id: str | None,
    console: Console,
) -> Session:
    """Resume a normally closed session, or start a new one."""
    if resume_id is None:
        session = Session(
            store=store,
            workspace=str(config.workspace),
            model=config.model,
            base_url=config.base_url,
        )
        console.print(Text(f"New session: {session.id}"))
        return session
    current_prompt = build_system_prompt(config.workspace)
    session = Session.resume(
        resume_id,
        store=store,
        workspace=str(config.workspace),
        model=config.model,
        base_url=config.base_url,
    )
    console.print(Text(f"Resumed session: {session.id}"))
    if session.model_changed:
        console.print(
            "Model or endpoint changed; using current configuration and resetting token calibration."
        )
    saved_prompts = [
        m.content for m in session.messages if isinstance(m, SystemMessage)
    ]
    if saved_prompts and saved_prompts != [current_prompt]:
        console.print(
            "System instructions changed; continuing with the archived instructions. Start a new session to use the current instructions."
        )
    return session


async def _main(argv: list[str] | None = None) -> int:
    """Load configuration and start the interactive or single-turn CLI."""
    args = _parse_args(argv or [])
    # In print mode every human-facing message goes to stderr so stdout stays clean.
    console = Console(stderr=args.print_prompt is not None)
    if args.list_sessions:
        return _list_sessions(console)
    try:
        config = load_runtime_config()
        loop = build_agent_loop(config, allow_all_tools=args.allow_all_tools)
    except (CodeheadError, ValueError, TypeError) as exc:
        console.print(Text(f"Error: {exc}", style="bold red"))
        return 1
    try:
        store = SQLiteSessionStore(config.session_db_path)
        try:
            session = _open_session(config, store, args.resume, console)
            if args.print_prompt is None:
                return await run_repl(loop, console=console, session=session)
            prompt = args.print_prompt or sys.stdin.read()
            if not prompt.strip():
                console.print(Text("Error: empty prompt", style="bold red"))
                return 1
            return await run_print(loop, session, prompt, console=console)
        finally:
            store.close()
    except (ResumeError, StorageError) as exc:
        console.print(Text(f"Error: {exc}", style="bold red"))
        return 1
    finally:
        await loop.aclose()


def _configure_utf8_stdio() -> None:
    """Bind the standard streams to UTF-8 so piped text is not locale-encoded."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    """Own one event loop for the entire CLI session."""
    _configure_utf8_stdio()
    try:
        return asyncio.run(_main(sys.argv[1:]))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
