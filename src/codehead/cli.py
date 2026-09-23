"""Command-line entry point for Codehead."""

import asyncio
from collections.abc import Callable
from contextlib import aclosing

from rich.console import Console
from rich.text import Text

from codehead.agent import Agent, AgentLoop
from codehead.config import Config
from codehead.errors import CodeheadError, StorageError
from codehead.llm import OpenAIProvider
from codehead.models import (
    AssistantTextDelta,
    ContextCompacted,
    ContextCompactionStarted,
    ModelCompleted,
    ToolCompleted,
    ToolStarted,
)
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


def build_agent_loop(config: Config) -> AgentLoop:
    """Construct the v0.1 agent and its built-in tools."""
    rg_executable = find_ripgrep()
    provider = OpenAIProvider(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        disable_thinking=True,
    )
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
    agent = Agent(
        provider=provider,
        tools=tools,
        system_prompt=build_system_prompt(config.workspace),
    )
    return AgentLoop(agent, context_budget=config.context_budget)


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

    while True:
        try:
            user_input = reader("[bold green]You>[/bold green] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            return 0

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
                    elif isinstance(event, ToolStarted):
                        tool_names[event.tool_call.id] = event.tool_call.name
                        console.print(Text(f"Tool> {event.tool_call.name}: started"))
                    elif isinstance(event, ToolCompleted):
                        name = tool_names.pop(event.result.tool_call_id)
                        status = "failed" if event.result.is_error else "succeeded"
                        console.print(Text(f"Tool> {name}: {status}"))
        except KeyboardInterrupt:
            console.print()
            return 0
        except asyncio.CancelledError:
            console.print()
            raise
        except StorageError as exc:
            if line_open:
                console.print()
            console.print(Text(f"Error: {exc}; session stopped", style="bold red"))
            return 1
        except CodeheadError as exc:
            if line_open:
                console.print()
            console.print(Text(f"Error: {exc}", style="bold red"))
            continue


async def _main() -> int:
    """Load configuration and start the interactive CLI."""
    console = Console()
    try:
        config = Config.from_env()
        loop = build_agent_loop(config)
    except (CodeheadError, ValueError) as exc:
        console.print(Text(f"Error: {exc}", style="bold red"))
        return 1
    try:
        store = SQLiteSessionStore(config.session_db_path)
        try:
            session = Session(
                store=store, workspace=str(config.workspace), model=config.model
            )
            return await run_repl(loop, console=console, session=session)
        finally:
            store.close()
    except StorageError as exc:
        console.print(Text(f"Error: {exc}", style="bold red"))
        return 1
    finally:
        await loop.aclose()


def main() -> int:
    """Own one event loop for the entire CLI session."""
    try:
        return asyncio.run(_main())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
