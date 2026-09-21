"""Core Codehead agent loop."""

from codehead.agent.agent import Agent
from codehead.errors import AgentAbortedError, ModelError, ToolExecutionError
from codehead.session import Message, Session, ToolResult


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
            session.add(Message(role="system", content=self._agent.system_prompt))
        session.add(Message(role="user", content=user_input))

        for _ in range(self._max_steps):
            assistant = self._agent.provider.complete(
                session.messages,
                self._agent.tools.schemas(),
            )
            if assistant.role != "assistant":
                raise ModelError("Model provider must return an assistant message")

            session.add(assistant)
            if not assistant.tool_calls:
                if assistant.content is None or not assistant.content.strip():
                    raise ModelError(
                        "Model response contained neither text nor tool calls"
                    )
                return assistant.content

            for tool_call in assistant.tool_calls:
                try:
                    result = self._agent.tools.execute(tool_call)
                except ToolExecutionError as exc:
                    result = ToolResult(
                        tool_call_id=tool_call.id,
                        output=str(exc),
                        is_error=True,
                    )
                session.add(Message(role="tool", tool_result=result))

        raise AgentAbortedError(
            f"Agent did not produce a final answer within {self._max_steps} steps"
        )
