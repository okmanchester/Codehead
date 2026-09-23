"""Core Codehead agent loop."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing

from codehead.agent.agent import Agent
from codehead.errors import AgentAbortedError, ModelError
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
    SystemMessage,
    ToolCompleted,
    ToolMessage,
    ToolStarted,
    TurnCompleted,
    UserMessage,
)
from codehead.session import Session
from codehead.tools.runtime import ToolRuntime


class AgentLoop:
    """Drive model responses and tool results until a final answer exists."""

    def __init__(self, agent: Agent, max_steps: int = 20) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self._agent = agent
        self._max_steps = max_steps
        self._tool_runtime = ToolRuntime(agent.tools)

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
        """Run one turn. Deltas are provisional; close the stream on early exit.

        A session supports only one active turn. Interrupted sessions are not
        resumable, and cancellation cannot stop an already running sync tool.
        """
        if len(session) == 0:
            session.add(SystemMessage(content=self._agent.system_prompt))
        session.add(UserMessage(content=user_input))

        for step in range(1, self._max_steps + 1):
            yield ModelStarted(step=step)
            response = None
            request = LLMRequest(
                messages=session.messages,
                tools=self._agent.tools.definitions(),
            )
            async with aclosing(self._agent.provider.stream(request)) as events:
                async for event in events:
                    if response is not None:
                        raise ModelError(
                            "Model emitted an event after its complete response"
                        )
                    if isinstance(event, LLMTextDelta):
                        yield AssistantTextDelta(step=step, delta=event.delta)
                    elif isinstance(event, LLMResponseCompleted):
                        response = event.response
            if response is None:
                raise ModelError("Model stream ended without a complete response")
            _validate_response(response)
            assistant = response.message
            session.add(assistant)
            yield ModelCompleted(step=step, response=response)
            if response.finish_reason == FinishReason.END_TURN:
                yield TurnCompleted(step=step, answer=assistant.content)
                return
            for tool_call in assistant.tool_calls:
                yield ToolStarted(step=step, tool_call=tool_call)
                result = await asyncio.to_thread(self._tool_runtime.execute, tool_call)
                session.add(
                    ToolMessage(
                        tool_call_id=result.tool_call_id,
                        content=result.output,
                        is_error=result.is_error,
                    )
                )

                yield ToolCompleted(step=step, result=result)

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
