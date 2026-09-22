"""Command-line entry point for Codehead."""

from collections.abc import Callable

from rich.console import Console
from rich.text import Text

from codehead.agent import Agent, AgentLoop
from codehead.config import Config
from codehead.errors import CodeheadError
from codehead.llm import OpenAIProvider
from codehead.prompt import build_system_prompt
from codehead.session import Session
from codehead.tools import BashTool, EditTool, ReadTool, ToolRegistry, WriteTool


def build_agent_loop(config: Config) -> AgentLoop:
    """Construct the v0.1 agent and its built-in tools."""
    provider = OpenAIProvider(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        disable_thinking=True,
    )
    tools = ToolRegistry(
        [
            ReadTool(config.workspace),
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
    return AgentLoop(agent)


def run_repl(
    loop: AgentLoop,
    *,
    console: Console,
    read_input: Callable[[str], str] | None = None,
) -> int:
    """Run an in-memory interactive session."""
    session = Session()
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

        try:
            answer = loop.run(session, user_input)
        except KeyboardInterrupt:
            console.print()
            return 0
        except CodeheadError as exc:
            console.print(Text(f"Error: {exc}", style="bold red"))
            return 1

        console.print(Text.assemble(("Codehead> ", "bold cyan"), answer))


def main() -> int:
    """Load configuration and start the interactive CLI."""
    console = Console()
    try:
        config = Config.from_env()
        loop = build_agent_loop(config)
    except (CodeheadError, ValueError) as exc:
        console.print(Text(f"Error: {exc}", style="bold red"))
        return 1
    return run_repl(loop, console=console)


if __name__ == "__main__":
    raise SystemExit(main())
