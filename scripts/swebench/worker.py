"""Run the existing AgentLoop inside a task container; never import the grader."""

import asyncio
import os
import sys
import time
from contextlib import aclosing
from pathlib import Path

from codehead.agent import Agent, AgentLoop
from codehead.errors import (
    AgentAbortedError,
    ContextError,
    ModelError,
    ToolInternalError,
)
from codehead.llm import OpenAIProvider
from codehead.models import ToolCompleted, TurnCompleted
from codehead.permissions import PermissionPolicy
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
from scripts.swebench.common import (
    TASK_INSTRUCTIONS,
    append_json,
    read_json,
    save_json,
)
from scripts.swebench.provider import MeteredProvider


async def execute(
    workspace: Path,
    output: Path,
    problem: str,
    settings: dict,
    provider,
    *,
    spent: float = 0,
    tools: ToolRegistry | None = None,
) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    meter = MeteredProvider(provider, settings, output, spent)
    started = time.monotonic()
    result = {"agent_status": "environment_failed", "reason": "worker setup incomplete"}
    loop = None
    session = None
    store = None
    try:
        if tools is None:
            rg = find_ripgrep()
            tools = ToolRegistry(
                [
                    ReadTool(workspace),
                    GlobTool(workspace, rg),
                    GrepTool(workspace, rg),
                    WriteTool(workspace),
                    EditTool(workspace),
                    BashTool(workspace, timeout_seconds=settings["shell_timeout"]),
                ]
            )
        loop = AgentLoop(
            Agent(meter, tools, build_system_prompt(workspace)),
            max_steps=settings["max_steps"],
            context_budget=settings["context_budget"],
            permission_policy=PermissionPolicy.allow_all(
                workspace, [d.name for d in tools.definitions()]
            ),
        )
        store = SQLiteSessionStore(output / "session.sqlite3")
        session = Session(
            store=store, workspace=str(workspace), model=settings["model"]
        )
        result = {"agent_status": "model_failed", "reason": "no terminal event"}
        async with asyncio.timeout(settings["agent_timeout"]):
            async with aclosing(
                loop.stream(session, TASK_INSTRUCTIONS + problem)
            ) as events:
                async for event in events:
                    append_json(output / "events.jsonl", event.model_dump(mode="json"))
                    if isinstance(event, ToolCompleted):
                        append_json(
                            output / "tools.jsonl", event.model_dump(mode="json")
                        )
                    if isinstance(event, TurnCompleted):
                        result = {"agent_status": "completed", "answer": event.answer}
    except TimeoutError:
        result = {"agent_status": "budget_exhausted", "reason": "agent_timeout"}
    except AgentAbortedError as exc:
        result = {"agent_status": "budget_exhausted", "reason": str(exc)}
    except ModelError as exc:
        result = {"agent_status": "model_failed", "reason": str(exc)}
    except ContextError as exc:
        result = {
            "agent_status": "model_failed"
            if isinstance(exc.__cause__, ModelError)
            else "budget_exhausted",
            "reason": str(exc),
        }
    except ToolInternalError as exc:
        result = {"agent_status": "tool_failed", "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - persist terminal state before exit
        result = {
            "agent_status": "environment_failed",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if meter.budget_reason:
            result.update(agent_status="budget_exhausted", reason=meter.budget_reason)
        # The loop settles its real tools before leaving the event stream.
        if session is not None and session.turn is not None:
            result["turn_status"] = session.turn.status
            result["tool_results"] = [
                {
                    "name": t.call.name,
                    "status": t.status,
                    "result": t.result.model_dump(mode="json") if t.result else None,
                }
                for t in session.turn.tools
            ]
        try:
            if session is not None:
                session.finish()
        except Exception as exc:  # noqa: BLE001 - cleanup failure is an environment result
            result.update(
                agent_status="environment_failed", reason=f"session cleanup: {exc}"
            )
        finally:
            if store is not None:
                store.close()
            try:
                await meter.aclose()
            except Exception as exc:  # noqa: BLE001 - always persist the accounting
                result.update(
                    agent_status="environment_failed", reason=f"provider cleanup: {exc}"
                )
            result["inference_seconds"] = time.monotonic() - started
            result["usage"] = meter.totals()
            save_json(output / "terminal.json", result)
    return result


def main():
    task = read_json(Path(sys.argv[1]))
    settings = task["settings"]
    provider = OpenAIProvider(
        model=settings["model"],
        api_key=os.environ.pop("DEEPSEEK_API_KEY"),
        base_url=settings["base_url"],
        disable_thinking=True,
        max_output_tokens=settings["max_output_tokens"],
    )
    asyncio.run(
        execute(
            Path("/testbed"),
            Path("/output"),
            task["problem_statement"],
            settings,
            provider,
            spent=task["prior_cost_cny"],
        )
    )


if __name__ == "__main__":
    main()
