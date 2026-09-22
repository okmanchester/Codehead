"""Core Codehead agent loop."""

from codehead.agent.agent import Agent
from codehead.errors import AgentAbortedError, ModelError, ToolExecutionError
from codehead.models import (
    FinishReason,
    LLMRequest,
    SystemMessage,
    ToolMessage,
    ToolResult,
    UserMessage,
)
from codehead.session import Session


class AgentLoop:
    """Drive model responses and tool results until a final answer exists."""

    def __init__(self, agent: Agent, max_steps: int = 20) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self._agent = agent
        self._max_steps = max_steps

    def run(self, session: Session, user_input: str) -> str:
        """Run one user turn against the supplied in-memory session."""
        if len(session) == 0:
            session.add(SystemMessage(content=self._agent.system_prompt))
        session.add(UserMessage(content=user_input))

        for _ in range(self._max_steps):
            response = self._agent.provider.complete(
                LLMRequest(
                    messages=session.messages,
                    tools=self._agent.tools.definitions(),
                )
            )
            assistant = response.message

            if response.finish_reason == FinishReason.END_TURN:
                if assistant.tool_calls:
                    raise ModelError("END_TURN response must not contain tool calls")
                if assistant.content is None or not assistant.content.strip():
                    raise ModelError(
                        "END_TURN response must contain non-blank text"
                    )
                session.add(assistant)
                return assistant.content

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

            session.add(assistant)
            for tool_call in assistant.tool_calls:
                try:
                    result = self._agent.tools.execute(tool_call)
                except ToolExecutionError as exc:
                    result = ToolResult(
                        tool_call_id=tool_call.id,
                        output=str(exc),
                        is_error=True,
                    )
                session.add(
                    ToolMessage(
                        tool_call_id=result.tool_call_id,
                        content=result.output,
                        is_error=result.is_error,
                    )
                )

        raise AgentAbortedError(
            f"Agent did not produce a final answer within {self._max_steps} steps"
        )
