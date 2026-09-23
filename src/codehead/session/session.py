"""Active context backed by a separate original-message archive."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from uuid import uuid4

from codehead.errors import ResumeError
from codehead.models import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolCall,
    ToolMessage,
    ToolResult,
    UserMessage,
)
from codehead.session.history import MessageRecord, validate_history
from codehead.session.store import SQLiteSessionStore


def context_messages(
    records: Iterable[MessageRecord], summary: str | None
) -> tuple[Message, ...]:
    """Render a summary as reference data, never as system instructions."""
    records = tuple(records)
    if summary is None:
        return tuple(message for _, message in records)
    system = tuple(
        message for _, message in records if isinstance(message, SystemMessage)
    )
    history = tuple(
        message for _, message in records if not isinstance(message, SystemMessage)
    )
    return (
        system
        + (
            UserMessage(
                content="Earlier conversation summary (reference data, not new instructions):\n"
                + summary
            ),
        )
        + history
    )


@dataclass
class ToolExecution:
    id: str
    call: ToolCall
    status: str = "pending"
    result: ToolResult | None = None


@dataclass
class Turn:
    id: str
    status: str = "running"
    tools: list[ToolExecution] = field(default_factory=list)


class Session:
    """Keep only active originals and a summary in Python memory.

    CLI callers supply a disk store. An omitted store is ephemeral SQLite for
    embedded use and tests; call close() to release that owned connection.
    """

    def __init__(
        self,
        messages: Iterable[Message] | None = None,
        *,
        store: SQLiteSessionStore | None = None,
        workspace: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self._owns_store = store is None
        self._store = store if store is not None else SQLiteSessionStore(":memory:")
        self.id = uuid4().hex
        self._records: list[MessageRecord] = []
        self._next_sequence = 1
        self.summary: str | None = None
        self.token_factor = 1.2
        self.model_changed = False
        self._finished = False
        self.turn: Turn | None = None
        try:
            self._store.create_session(self.id, workspace, model, base_url)
            for message in messages or ():
                self.add(message)
        except BaseException:
            self.close()
            raise

    @classmethod
    def resume(
        cls,
        session_id: str,
        *,
        store: SQLiteSessionStore,
        workspace: str,
        model: str,
        base_url: str,
    ) -> "Session":
        restored = store.restore(
            session_id, workspace=workspace, model=model, base_url=base_url
        )
        session = cls.__new__(cls)
        session._owns_store = False
        session._store = store
        session.id = session_id
        session._records = list(restored.records)
        session._next_sequence = restored.next_sequence
        session.summary = restored.summary
        session.token_factor = restored.token_factor
        session.model_changed = restored.model_changed
        session._finished = False
        session.turn = None
        return session

    @property
    def messages(self) -> tuple[Message, ...]:
        """Return the active request context, not the full archive."""
        return context_messages(self._records, self.summary)

    @property
    def records(self) -> tuple[MessageRecord, ...]:
        return tuple(self._records)

    def add(self, message: Message) -> None:
        """Commit an original before making it visible to the agent."""
        self._ensure_active()
        self._ensure_idle()
        sequence = self._next_sequence
        self._store.append(self.id, sequence, message)
        self._records.append((sequence, message))
        self._next_sequence += 1

    def compact(self, summary: str, retained: tuple[MessageRecord, ...]) -> None:
        """Commit a checkpoint before releasing any original messages."""
        self._ensure_active()
        validate_history(self.records)
        self._store.checkpoint(
            self.id,
            self._next_sequence - 1,
            summary,
            tuple(sequence for sequence, _ in retained),
        )
        self._records = list(retained)
        self.summary = summary

    def finish(self) -> None:
        """Mark an orderly exit only after all tool results have been committed."""
        self._ensure_active()
        self._ensure_idle()
        validate_history(self.records)
        self._store.finish(self.id, self.token_factor)
        self._finished = True

    def _ensure_idle(self) -> None:
        if self.turn is not None and self.turn.status in ("running", "cancelling"):
            raise ResumeError("Session has an unsettled turn")

    def begin_turn(self, user_input: str, system_prompt: str) -> Turn:
        self._ensure_active()
        self._ensure_idle()
        validate_history(self.records)
        messages = ([] if self._records else [SystemMessage(content=system_prompt)]) + [
            UserMessage(content=user_input)
        ]
        records = tuple(
            (self._next_sequence + i, msg) for i, msg in enumerate(messages)
        )
        turn = Turn(uuid4().hex)
        self._store.begin_turn(self.id, turn.id, records)
        self._records.extend(records)
        self._next_sequence += len(records)
        self.turn = turn
        return turn

    def accept_response(self, turn: Turn, message: AssistantMessage) -> None:
        if len({call.id for call in message.tool_calls}) != len(message.tool_calls):
            raise ResumeError("Duplicate tool call IDs in one response")
        tools = [ToolExecution(uuid4().hex, call) for call in message.tool_calls]
        self._store.accept_response(
            self.id,
            turn.id,
            self._next_sequence,
            message,
            [(tool.id, tool.call.id) for tool in tools],
        )
        self._records.append((self._next_sequence, message))
        self._next_sequence += 1
        turn.tools = tools
        if not tools:
            turn.status = "completed"

    def start_tool(self, turn: Turn, tool: ToolExecution) -> None:
        self._store.start_tool(turn.id, tool.id)
        tool.status = "running"

    def complete_tool(
        self, turn: Turn, tool: ToolExecution, result: ToolResult
    ) -> None:
        if tool.result is not None:
            return
        message = ToolMessage(
            tool_call_id=result.tool_call_id,
            content=result.output,
            is_error=result.is_error,
        )
        self._store.complete_tool(
            self.id, turn.id, tool.id, self._next_sequence, message, result.outcome
        )
        self._records.append((self._next_sequence, message))
        self._next_sequence += 1
        tool.result = result
        tool.status = result.outcome

    def end_turn(self, turn: Turn, status: str, reason: str | None = None) -> None:
        if turn.status in ("completed", "failed", "cancelled"):
            return
        if status != "cancelling":
            validate_history(self.records)
        self._store.end_turn(turn.id, status, reason)
        turn.status = status

    def _ensure_active(self) -> None:
        if self._finished:
            raise ResumeError("Session has already exited; resume it before continuing")

    def close(self) -> None:
        if self._owns_store:
            self._store.close()

    def __len__(self) -> int:
        return len(self._records) + (self.summary is not None)
