"""Core Codehead agent loop."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing

from codehead.agent.agent import Agent
from codehead.agent.cancellation import TurnControl, settle
from codehead.agent.context import ContextManager, observe_usage
from codehead.errors import (
    AgentAbortedError,
    ModelError,
    StorageError,
    ToolCleanupError,
)
from codehead.models import (
    AgentEvent,
    AssistantTextDelta,
    FinishReason,
    LLMRequest,
    LLMResponse,
    LLMResponseCompleted,
    LLMTextDelta,
    ModelCompleted,
    ModelStarted,
    ToolCompleted,
    ToolResult,
    ToolStarted,
    TurnCancelled,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from codehead.session import Session
from codehead.session.session import Turn
from codehead.tools.runtime import ToolRuntime


class AgentLoop:
    """Drive model responses and tool results until a final answer exists."""

    def __init__(
        self, agent: Agent, max_steps: int = 20, *, context_budget: int = 32768
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self._agent = agent
        self._max_steps = max_steps
        self._tool_runtime = ToolRuntime(agent.tools)
        self._context = ContextManager(agent.provider, context_budget)
        self._controls: dict[str, TurnControl] = {}

    async def aclose(self) -> None:
        """Close the provider after all turns have finished."""
        await self._agent.provider.aclose()

    async def run(self, session: Session, user_input: str) -> str:
        """Consume one turn's events and return its final answer."""
        answer = None
        async with aclosing(self.stream(session, user_input)) as events:
            async for event in events:
                if isinstance(event, TurnCompleted):
                    answer = event.answer
        if answer is None:
            raise AgentAbortedError("Agent stream ended without a final answer")
        return answer

    async def stream(
        self, session: Session, user_input: str
    ) -> AsyncIterator[AgentEvent]:
        """Close early consumers with aclosing(); cancellation settles owned work."""
        turn = session.begin_turn(user_input, self._agent.system_prompt)
        control = TurnControl(owner=asyncio.current_task())
        self._controls[session.id] = control
        iterator = self._stream(session, turn, control)
        try:
            yield TurnStarted(turn_id=turn.id)
            while True:
                control.operation = asyncio.create_task(_advance(iterator))
                try:
                    event = await asyncio.shield(control.operation)
                except StopAsyncIteration:
                    break
                yield event.model_copy(update={"turn_id": turn.id})
        except BaseException as exc:
            control.cancel.set()
            control.cleanup = asyncio.create_task(
                self._close_turn(iterator, session, turn, control, exc)
            )
            await settle(control.cleanup)
            if isinstance(exc, asyncio.CancelledError) and control.requested:
                if turn.status == "completed":
                    return
                yield TurnCancelled(turn_id=turn.id)
                return
            if isinstance(exc, Exception) and not isinstance(
                exc, (StorageError, ToolCleanupError)
            ):
                yield TurnFailed(turn_id=turn.id, reason=str(exc))
            raise
        finally:
            self._controls.pop(session.id, None)

    def request_cancel(self, session: Session) -> bool:
        """Request a controlled interruption; repeated requests are harmless."""
        control = self._controls.get(session.id)
        if control is None:
            return False
        if session.turn is not None and session.turn.status == "completed":
            return True
        if not control.cancel.is_set():
            control.requested = True
            control.cancel.set()
            control.owner.cancel()
        return True

    async def _close_turn(
        self,
        iterator,
        session: Session,
        turn: Turn,
        control: TurnControl,
        error: BaseException,
    ) -> None:
        if control.operation is not None:
            if not control.operation.done():
                control.operation.cancel()
            try:
                await control.operation
            except (asyncio.CancelledError, StopAsyncIteration):
                pass
            except (StorageError, ToolCleanupError) as exc:
                error = exc
            except Exception as exc:  # noqa: BLE001 - original interruption wins
                if not isinstance(error, (asyncio.CancelledError, GeneratorExit)):
                    error = exc
        try:
            await iterator.aclose()
        finally:
            await self._cleanup(session, turn, control, error)

    def cancellation_status(self, session: Session) -> str:
        control = self._controls.get(session.id)
        if (
            control is not None
            and control.worker is not None
            and not control.worker.done()
            and control.execution.call.name not in {"bash", "glob", "grep"}
        ):
            return "正在停止；等待当前文件操作结束"
        return "正在停止；等待回合收尾"

    async def _cleanup(
        self, session: Session, turn: Turn, control: TurnControl, error: BaseException
    ) -> None:
        if turn.status == "completed":
            return
        cancelled = isinstance(
            error, (asyncio.CancelledError, GeneratorExit, KeyboardInterrupt)
        )
        storage_error = None
        if cancelled:
            try:
                session.end_turn(turn, "cancelling")
            except StorageError as exc:
                storage_error = exc
        # Even when the archive failed, never abandon an executing thread.
        result = await settle(control.worker) if control.worker is not None else None
        if storage_error is not None:
            raise storage_error
        if isinstance(error, (StorageError, ToolCleanupError)):
            raise error
        if result is not None:
            session.complete_tool(turn, control.execution, result)
        for execution in turn.tools:
            if execution.result is None:
                session.complete_tool(
                    turn,
                    execution,
                    ToolResult(
                        tool_call_id=execution.call.id,
                        output="Skipped: turn stopped before execution; tool did not run",
                        outcome="skipped",
                    ),
                )
        session.end_turn(
            turn, "cancelled" if cancelled else "failed", str(error) or None
        )

    async def _stream(
        self, session: Session, turn: Turn, control: TurnControl
    ) -> AsyncIterator[AgentEvent]:
        for step in range(1, self._max_steps + 1):
            control.check()
            tools = self._agent.tools.definitions()
            async with aclosing(self._context.prepare(session, tools, step)) as events:
                async for event in events:
                    yield event
                    control.check()
            yield ModelStarted(step=step)
            control.check()
            response = None
            request = LLMRequest(messages=session.messages, tools=tools)
            async with aclosing(self._agent.provider.stream(request)) as events:
                async for event in events:
                    control.check()
                    if response is not None:
                        raise ModelError(
                            "Model emitted an event after its complete response"
                        )
                    if isinstance(event, LLMTextDelta):
                        yield AssistantTextDelta(step=step, delta=event.delta)
                    elif isinstance(event, LLMResponseCompleted):
                        response = event.response
            control.check()
            if response is None:
                raise ModelError("Model stream ended without a complete response")
            _validate_response(response)
            assistant = response.message
            session.accept_response(turn, assistant)
            observe_usage(session, request, response.usage)
            yield ModelCompleted(step=step, response=response)
            if response.finish_reason == FinishReason.END_TURN:
                yield TurnCompleted(step=step, answer=assistant.content)
                return
            for execution in turn.tools:
                control.check()
                yield ToolStarted(
                    step=step, tool_call=execution.call, execution_id=execution.id
                )
                control.check()
                session.start_tool(turn, execution)
                control.execution = execution
                control.worker = self._tool_runtime.start(
                    execution.call, control.cancel
                )
                result = await asyncio.shield(control.worker)
                session.complete_tool(turn, execution, result)
                control.worker = None
                control.execution = None
                yield ToolCompleted(step=step, result=result, execution_id=execution.id)
                control.check()
        raise AgentAbortedError(
            f"Agent did not produce a final answer within {self._max_steps} steps"
        )


def _validate_response(response: LLMResponse) -> None:
    assistant = response.message
    if response.finish_reason == FinishReason.END_TURN:
        if assistant.tool_calls:
            raise ModelError("END_TURN response must not contain tool calls")
        if assistant.content is None or not assistant.content.strip():
            raise ModelError("END_TURN response must contain non-blank text")
        return

    if response.finish_reason == FinishReason.MAX_TOKENS:
        raise ModelError("Model output was truncated (MAX_TOKENS)")
    if response.finish_reason == FinishReason.CONTENT_FILTER:
        raise ModelError("Model response was filtered (CONTENT_FILTER)")
    if response.finish_reason == FinishReason.UNKNOWN:
        raise ModelError("Model returned an unknown finish reason (UNKNOWN)")
    if response.finish_reason != FinishReason.TOOL_CALLS:
        raise ModelError(f"Unsupported finish reason: {response.finish_reason}")
    if not assistant.tool_calls:
        raise ModelError("TOOL_CALLS response must contain tool calls")

    if len({call.id for call in assistant.tool_calls}) != len(assistant.tool_calls):
        raise ModelError("Duplicate tool call IDs in one response")


async def _advance(iterator):
    try:
        return await anext(iterator)
    except KeyboardInterrupt as exc:
        raise asyncio.CancelledError from exc
    except SystemExit as exc:
        raise ToolCleanupError("Execution exited before a confirmed result") from exc
