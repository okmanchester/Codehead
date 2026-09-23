"""Validation shared by orderly exit and archived context restoration."""

from codehead.errors import ResumeError
from codehead.models import AssistantMessage, Message, ToolMessage

type MessageRecord = tuple[int, Message]


def validate_history(records: tuple[MessageRecord, ...]) -> None:
    """Accept unanswered users and completed tool groups, but no orphan results."""
    previous = 0
    pending: set[str] = set()
    for sequence, message in records:
        if type(sequence) is not int or sequence <= previous:
            raise ResumeError("Session history has invalid message sequences")
        previous = sequence
        if isinstance(message, ToolMessage):
            if message.tool_call_id not in pending:
                raise ResumeError(
                    "Session history has an orphan or duplicate tool result"
                )
            pending.remove(message.tool_call_id)
            continue
        if pending:
            raise ResumeError("Session history has missing tool results")
        if isinstance(message, AssistantMessage):
            if message.tool_calls:
                pending = {call.id for call in message.tool_calls}
                if len(pending) != len(message.tool_calls):
                    raise ResumeError("Session history has duplicate tool call IDs")
            elif not message.content or not message.content.strip():
                raise ResumeError("Session history has an invalid assistant response")
    if pending:
        raise ResumeError("Session history has missing tool results")
